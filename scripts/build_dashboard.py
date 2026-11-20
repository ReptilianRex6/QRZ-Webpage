#!/usr/bin/env python3
"""Build a static HF dashboard with no client-side JavaScript.

GitHub Pages is loaded inside a QRZ iframe whose sandbox does not include
allow-scripts, and nested frames inherit that sandbox. This script runs in
GitHub Actions, fetches propagation data, and writes index.html with the
numbers already in the markup.
"""

from __future__ import annotations

import json
import math
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = Path(__file__).with_name('dashboard_template.html')
DATA_PATH = ROOT / 'data' / 'conditions.json'
PAGE_PATH = ROOT / 'index.html'
UA = 'KM7CPJ-propagation-dashboard/1.0 (+https://reptilianrex6.github.io/QRZ-Webpage/)'

BANDS = (80, 40, 20, 15, 10)
FREQ = {
    80: '3.5–4.0 MHz',
    40: '7.0–7.3 MHz',
    20: '14.0–14.35 MHz',
    15: '21.0–21.45 MHz',
    10: '28.0–29.7 MHz',
}
# N0NBH publishes four band groups. 80m and 40m share the low group.
GROUP = {
    80: '80m-40m',
    40: '80m-40m',
    20: '30m-20m',
    15: '17m-15m',
    10: '12m-10m',
}
CENTERS = {80: 3.6, 40: 7.1, 20: 14.15, 15: 21.2, 10: 28.5}
LEVELS = ('Poor', 'Fair', 'Good')
KNOWN = {'excellent', 'good', 'fair', 'marginal', 'poor', 'closed'}
HAMQSL_URL = 'https://www.hamqsl.com/solarxml.php'
NOAA = {
    'flux': 'https://services.swpc.noaa.gov/products/summary/10cm-flux.json',
    'kp': 'https://services.swpc.noaa.gov/json/planetary_k_index_1m.json',
    'kp3h': 'https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json',
    'xray': 'https://services.swpc.noaa.gov/json/goes/primary/xrays-6-hour.json',
}


class SourceError(Exception):
    pass


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            if response.status != 200:
                raise SourceError(f'HTTP {response.status} for {url}')
            return response.read()
    except urllib.error.URLError as exc:
        raise SourceError(str(exc)) from exc


def esc(value: object) -> str:
    text = '' if value is None else str(value)
    return (
        text.replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
    )


def condition(value: object) -> str:
    text = str(value).strip() if value else '—'
    kind = text.lower()
    css = kind if kind in KNOWN else 'unknown'
    return f'<span class="condition {css}">{esc(text or "—")}</span>'


def format_kp(value: float) -> str:
    rounded = math.floor(value * 100 + 0.5) / 100
    text = f'{rounded:.2f}'
    if text.endswith('.00'):
        return text[:-3]
    if text.endswith('0'):
        return text[:-1]
    return text


def format_flux(value: float) -> str:
    if abs(value - round(value)) < 0.05:
        return str(int(round(value)))
    return f'{value:.1f}'


def parse_noaa_time(value: str | None) -> str:
    if not value:
        return 'time unavailable'
    raw = value.strip().replace('Z', '')
    try:
        moment = datetime.strptime(raw[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
    except ValueError:
        return value.strip()
    return moment.strftime('%d %b %Y %H:%M UTC')


def lower(level: str, steps: int) -> str:
    index = LEVELS.index(level) if level in LEVELS else 0
    return LEVELS[max(0, index - steps)]


def cap(level: str, maximum: str) -> str:
    return LEVELS[min(LEVELS.index(level), LEVELS.index(maximum))]


def rating(muf: float, mhz: float) -> str:
    ratio = muf / mhz
    if ratio >= 1.2:
        return 'Good'
    if ratio >= 0.7:
        return 'Fair'
    return 'Poor'


def estimate_bands(sfi: float, kp: float, a_index: float | None, xray: float | None) -> list[dict]:
    """Same simple MUF model previously used for the NOAA fallback."""
    muf_day = 8 + 0.12 * sfi
    muf_night = 0.65 * muf_day
    storm = 2 if kp >= 7 else 1 if kp >= 5 else 0
    flare = 0
    if xray is not None and math.isfinite(xray):
        flare = 2 if xray >= 1e-4 else 1 if xray >= 5e-5 else 0
    rows = []
    for band in BANDS:
        mhz = CENTERS[band]
        day = rating(muf_day, mhz)
        if mhz < 4:
            day = cap(day, 'Poor')
        elif mhz < 8:
            day = cap(day, 'Fair')
        night = rating(muf_night, mhz)
        if storm:
            day = lower(day, storm)
            night = lower(night, storm)
        elif kp >= 4 and mhz < 10:
            night = lower(night, 1)
        if a_index is not None and a_index >= 15 and kp < 5 and mhz < 10:
            night = lower(night, 1)
        if flare:
            day = lower(day, flare)
        rows.append({'m': band, 'day': day, 'night': night})
    return rows


def geomagnetic(kp: float, a_index: float | None) -> str:
    if kp >= 5:
        return 'STORM'
    if kp >= 4:
        return 'ACTIVE'
    if kp >= 3 or (a_index is not None and a_index >= 15):
        return 'UNSETTLED'
    return 'QUIET'


def rows_of(payload: object) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return [payload]
    return []


def last_numeric(rows: list, key: str) -> tuple[float | None, str | None]:
    for row in reversed(rows):
        if not isinstance(row, dict) or key not in row:
            continue
        try:
            value = float(row[key])
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            return value, row.get('time_tag')
    return None, None


def fetch_hamqsl() -> dict:
    root = ET.fromstring(fetch_bytes(HAMQSL_URL))
    solar = root.find('solardata')
    if solar is None:
        raise SourceError('hamqsl XML has no solardata')
    groups: dict[tuple[str, str], str] = {}
    for node in solar.findall('./calculatedconditions/band'):
        name = (node.get('name') or '').strip().lower()
        when = (node.get('time') or '').strip().lower()
        text = (node.text or '').strip()
        if name and when and text:
            groups[(name, when)] = text
    bands = []
    for band in BANDS:
        group = GROUP[band]
        day = groups.get((group, 'day'))
        night = groups.get((group, 'night'))
        if not day or not night:
            raise SourceError(f'hamqsl is missing {group}')
        bands.append({'m': band, 'day': day, 'night': night})

    def field(tag: str) -> str:
        node = solar.find(tag)
        return (node.text or '').strip() if node is not None and node.text else ''

    sfi = field('solarflux')
    k_index = field('kindex')
    a_index = field('aindex')
    geo = field('geomagfield')
    updated = field('updated')
    if not sfi or not k_index or not updated:
        raise SourceError('hamqsl is missing solar flux, K-index, or time')
    return {
        'source': 'n0nbh',
        'stale': False,
        'updated': updated,
        'sfi': sfi,
        'k': k_index,
        'a': a_index or '—',
        'geo': geo or '—',
        'bands': bands,
    }


def fetch_noaa() -> dict:
    flux_rows = rows_of(json.loads(fetch_bytes(NOAA['flux'])))
    sfi, _sfi_time = last_numeric(flux_rows, 'flux')
    if sfi is None:
        raise SourceError('NOAA solar flux missing')
    try:
        kp_rows = rows_of(json.loads(fetch_bytes(NOAA['kp'])))
    except SourceError:
        kp_rows = []
    kp, kp_time = last_numeric(kp_rows, 'estimated_kp')
    try:
        kp3_rows = rows_of(json.loads(fetch_bytes(NOAA['kp3h'])))
    except SourceError:
        kp3_rows = []
    if kp is None:
        kp, kp_time = last_numeric(kp3_rows, 'Kp')
    if kp is None:
        raise SourceError('NOAA K-index missing')
    recent = []
    for row in kp3_rows:
        if isinstance(row, dict):
            try:
                recent.append(float(row['a_running']))
            except (KeyError, TypeError, ValueError):
                continue
    recent = [value for value in recent if math.isfinite(value)][-8:]
    a_index = round(sum(recent) / len(recent)) if recent else None
    xray = None
    try:
        xray_rows = [
            row for row in rows_of(json.loads(fetch_bytes(NOAA['xray'])))
            if isinstance(row, dict) and row.get('energy') == '0.1-0.8nm'
        ]
        xray, _xray_time = last_numeric(xray_rows, 'flux')
    except SourceError:
        xray = None
    return {
        'source': 'noaa',
        'stale': False,
        'updated': parse_noaa_time(kp_time),
        'sfi': format_flux(sfi),
        'k': format_kp(kp),
        'a': '—' if a_index is None else str(a_index),
        'geo': geomagnetic(kp, a_index),
        'bands': estimate_bands(sfi, kp, a_index, xray),
    }


def load_previous() -> dict | None:
    if not DATA_PATH.exists():
        return None
    try:
        data = json.loads(DATA_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get('source') not in {'n0nbh', 'noaa'}:
        return None
    if not isinstance(data.get('bands'), list) or len(data['bands']) != len(BANDS):
        return None
    return data


def unavailable() -> dict:
    return {
        'source': 'none',
        'stale': True,
        'updated': '',
        'sfi': '—',
        'k': '—',
        'a': '—',
        'geo': '—',
        'bands': [{'m': band, 'day': '—', 'night': '—'} for band in BANDS],
    }


def presentation(snapshot: dict) -> dict:
    source = snapshot.get('source')
    stale = bool(snapshot.get('stale'))
    if source == 'none':
        pill_class, pill = 'err', '● DATA UNAVAILABLE'
        section = 'HF band conditions'
        note = 'Live data could not be loaded from N0NBH or NOAA. Open the source link to check current conditions.'
        updated = 'No saved reading is available.'
        credit = (
            'Data: <a href="https://www.hamqsl.com/solar.html" target="_blank" '
            'rel="noopener noreferrer">N0NBH</a> · hamqsl.com'
        )
    elif stale and source == 'noaa':
        pill_class, pill = 'stale', '● STALE DATA'
        section = 'Estimated HF band conditions'
        note = 'Live update failed. Showing the last NOAA estimate, not a contact report.'
        updated = f"Live update failed. Last good estimate: {snapshot.get('updated') or 'time unavailable'}"
        credit = (
            'Last estimate: <a href="https://www.swpc.noaa.gov/" target="_blank" '
            'rel="noopener noreferrer">NOAA SWPC</a>'
        )
    elif stale:
        pill_class, pill = 'stale', '● STALE DATA'
        section = 'HF band conditions'
        note = 'Live update failed. Showing the last N0NBH reading. 80m and 40m share one group. Not a guarantee of a contact.'
        updated = f"Live update failed. Last good reading: {snapshot.get('updated') or 'time unavailable'}"
        credit = (
            'Last good data: <a href="https://www.hamqsl.com/solar.html" target="_blank" '
            'rel="noopener noreferrer">N0NBH</a> · hamqsl.com'
        )
    elif source == 'noaa':
        pill_class, pill = 'estimate', '● NOAA ESTIMATE'
        section = 'Estimated HF band conditions'
        note = 'N0NBH was unavailable. These day/night ratings are NOAA-based estimates, not contact reports.'
        updated = f"Updated: {snapshot.get('updated') or 'time unavailable'}"
        credit = (
            'Estimate: <a href="https://www.swpc.noaa.gov/" target="_blank" '
            'rel="noopener noreferrer">NOAA SWPC</a>'
        )
    else:
        pill_class, pill = '', '● DATA LOADED'
        section = 'HF band conditions'
        note = 'N0NBH day/night ratings. 80m and 40m share the 80–40 m group. Not a guarantee of a contact.'
        updated = f"Updated: {snapshot.get('updated') or 'time unavailable'}"
        credit = (
            'Data: <a href="https://www.hamqsl.com/solar.html" target="_blank" '
            'rel="noopener noreferrer">N0NBH</a> · hamqsl.com'
        )
    by_band = {int(row['m']): row for row in snapshot.get('bands', [])}
    rows = []
    for band in BANDS:
        row = by_band.get(band, {})
        rows.append(
            '<div class="band"><div><div class="bandname">'
            f'{band}m</div><span class="freq">{esc(FREQ[band])}</span></div>'
            f'{condition(row.get("day"))}{condition(row.get("night"))}</div>'
        )
    return {
        'PILL_CLASS': '' if not pill_class else ' ' + pill_class,
        'PILL': esc(pill),
        'SECTION': esc(section),
        'NOTE': esc(note),
        'UPDATED': esc(updated),
        'CREDIT': credit,
        'SFI': esc(snapshot.get('sfi') or '—'),
        'K': esc(snapshot.get('k') or '—'),
        'A': esc(snapshot.get('a') or '—'),
        'GEO': esc(snapshot.get('geo') or '—'),
        'BANDS': ''.join(rows),
    }


def render(snapshot: dict) -> str:
    html = TEMPLATE.read_text()
    for key, value in presentation(snapshot).items():
        html = html.replace('{{' + key + '}}', value)
    if '{{' in html:
        raise SystemExit('dashboard template has an unfilled placeholder')
    if '<script' in html.lower():
        raise SystemExit('dashboard HTML must not contain script')
    return html


def write_if_changed(path: Path, text: str) -> bool:
    if path.exists() and path.read_text() == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return True


def canonical(snapshot: dict) -> dict:
    return {
        'source': snapshot['source'],
        'stale': bool(snapshot['stale']),
        'updated': snapshot.get('updated') or '',
        'sfi': snapshot.get('sfi') or '—',
        'k': snapshot.get('k') or '—',
        'a': snapshot.get('a') or '—',
        'geo': snapshot.get('geo') or '—',
        'bands': [
            {'m': int(row['m']), 'day': row.get('day') or '—', 'night': row.get('night') or '—'}
            for row in snapshot['bands']
        ],
    }


def main() -> int:
    snapshot = None
    try:
        snapshot = fetch_hamqsl()
        print('source n0nbh', snapshot['updated'], 'SFI', snapshot['sfi'], 'K', snapshot['k'])
    except Exception as exc:  # network, parse, or incomplete feed
        print(f'hamqsl failed: {exc}', file=sys.stderr)
        try:
            snapshot = fetch_noaa()
            print('source noaa', snapshot['updated'], 'SFI', snapshot['sfi'], 'K', snapshot['k'])
        except Exception as backup_exc:
            print(f'noaa failed: {backup_exc}', file=sys.stderr)
    previous = load_previous()
    if snapshot is None:
        if previous:
            snapshot = dict(previous)
            snapshot['stale'] = True
            print('keeping last good data')
        else:
            snapshot = unavailable()
            print('no saved reading')
    else:
        snapshot['stale'] = False
    normalized = canonical(snapshot)
    page = render(normalized)
    changed = write_if_changed(PAGE_PATH, page)
    if normalized['source'] != 'none':
        blob = json.dumps(normalized, indent=2) + '\n'
        changed = write_if_changed(DATA_PATH, blob) or changed
    print('updated' if changed else 'unchanged')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
