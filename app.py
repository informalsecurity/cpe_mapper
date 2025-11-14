#!/usr/bin/env python3
"""
CPE Mapping Service - Flask API
Provides CPE lookups for installed software with NVD integration and LLM fallback
"""

import os
import re
import time
import sqlite3
import requests
from datetime import datetime
from flask import Flask, request, jsonify
from contextlib import contextmanager

app = Flask(__name__)

# Configuration
DATABASE_PATH = os.getenv('DATABASE_PATH', '/data/cpe_mappings.db')
NVD_API_KEY = os.getenv('NVD_API_KEY', '')  # Optional, increases rate limit
LLM_API_KEY = os.getenv('LLM_API_KEY', '')  # Anthropic API key for fallback
RATE_LIMIT_DELAY = 6.0 if not NVD_API_KEY else 0.6  # Seconds between NVD requests

# Rate limiting tracking
last_nvd_request = 0

def init_database():
    """Initialize SQLite database with schema"""
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS cpe_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_name TEXT NOT NULL UNIQUE,
            normalized_name TEXT,
            matched_name TEXT,
            publisher TEXT,
            version TEXT,
            cpe TEXT,
            vendor TEXT,
            product TEXT,
            match_method TEXT,
            confidence_score REAL,
            date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_verified TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            times_queried INTEGER DEFAULT 1,
            notes TEXT
        )
    ''')

    # Index for faster lookups
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_original_name ON cpe_mappings(original_name)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_match_method ON cpe_mappings(match_method)')

    conn.commit()
    conn.close()

# Initialize database on module load
init_database()

@contextmanager
def get_db():
    """Context manager for database connections"""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def normalize_app_name(name):
    """Normalize application name by removing common patterns"""
    if not name:
        return ""

    normalized = name

    # Remove trademark symbols
    normalized = re.sub(r'\(R\)', '', normalized)
    normalized = re.sub(r'\(TM\)', '', normalized)
    normalized = re.sub(r'®', '', normalized)
    normalized = re.sub(r'™', '', normalized)

    # Remove language codes
    normalized = re.sub(r'\s*\(?\s*(en-us|en-US|x64 en-US|en_US)\s*\)?', '', normalized, flags=re.IGNORECASE)

    # Remove architecture indicators
    normalized = re.sub(r'\s*\(\s*(x64|x86|ARM64|64-bit|32-bit|amd64)\s*\)', '', normalized, flags=re.IGNORECASE)

    # Remove version numbers at the end
    normalized = re.sub(r'\s+\d+(\.\d+)*(\s+.*)?$', '', normalized)

    # Remove update/edition indicators
    normalized = re.sub(r'\s+(Update|Redistributable|Runtime|Platform|Service Pack|SP\d+).*$', '', normalized, flags=re.IGNORECASE)

    # Clean up whitespace
    normalized = re.sub(r'\s+', ' ', normalized).strip()

    return normalized

def query_nvd_cpe(search_term, max_results=5):
    """Query NVD API for CPE matches"""
    global last_nvd_request

    # Rate limiting
    elapsed = time.time() - last_nvd_request
    if elapsed < RATE_LIMIT_DELAY:
        time.sleep(RATE_LIMIT_DELAY - elapsed)

    url = f"https://services.nvd.nist.gov/rest/json/cpes/2.0"
    params = {
        'keywordSearch': search_term,
        'resultsPerPage': max_results
    }

    headers = {}
    if NVD_API_KEY:
        headers['apiKey'] = NVD_API_KEY

    try:
        last_nvd_request = time.time()
        response = requests.get(url, params=params, headers=headers, timeout=30)

        if response.status_code == 429:
            # Rate limited - wait and retry once
            time.sleep(35)
            last_nvd_request = time.time()
            response = requests.get(url, params=params, headers=headers, timeout=30)

        response.raise_for_status()
        data = response.json()

        results = []
        if 'products' in data:
            for product in data['products']:
                cpe_name = product['cpe']['cpeName']

                # Extract vendor and product from CPE
                # CPE format: cpe:2.3:a:vendor:product:version:...
                parts = cpe_name.split(':')
                if len(parts) >= 5:
                    vendor = parts[3]
                    product_name = parts[4]

                    results.append({
                        'cpe': cpe_name,
                        'vendor': vendor,
                        'product': product_name
                    })

        return results

    except Exception as e:
        print(f"NVD API error: {e}")
        return []

def backoff_search(app_name):
    """Perform backoff search by removing words from right to left"""
    words = app_name.split()

    for i in range(len(words), 0, -1):
        search_term = ' '.join(words[:i])
        print(f"  Trying: {search_term}")

        results = query_nvd_cpe(search_term, max_results=1)
        if results:
            return results[0], search_term

    return None, None

def llm_cpe_lookup(app_name, publisher):
    """Use LLM to find CPE as last resort"""
    if not LLM_API_KEY:
        return None

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=LLM_API_KEY)

        prompt = f"""Given this installed software:
Name: {app_name}
Publisher: {publisher}

What is the correct CPE 2.3 identifier for this software? Respond with ONLY the CPE string in this format:
cpe:2.3:a:vendor:product:version:*:*:*:*:*:*:*

If you cannot determine it with high confidence, respond with: UNKNOWN"""

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}]
        )

        response_text = message.content[0].text.strip()

        if response_text.startswith('cpe:2.3:'):
            # Extract vendor and product from CPE
            parts = response_text.split(':')
            if len(parts) >= 5:
                return {
                    'cpe': response_text,
                    'vendor': parts[3],
                    'product': parts[4]
                }

        return None

    except Exception as e:
        print(f"LLM lookup error: {e}")
        return None

def lookup_cpe(app_data):
    """Main CPE lookup logic"""
    app_name = app_data.get('Name', '')
    publisher = app_data.get('Publisher', '')
    version = app_data.get('Version', '')

    if not app_name:
        return None

    # Check if we already have this in database
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM cpe_mappings WHERE original_name = ?', (app_name,))
        existing = cursor.fetchone()

        if existing:
            # Update query count
            cursor.execute('''
                UPDATE cpe_mappings
                SET times_queried = times_queried + 1, last_verified = CURRENT_TIMESTAMP
                WHERE original_name = ?
            ''', (app_name,))
            conn.commit()

            # Return existing result (even if NULL/not found)
            return {
                'cpe': existing['cpe'],
                'vendor': existing['vendor'],
                'product': existing['product'],
                'match_method': existing['match_method'],
                'cached': True
            }

    # Not in database - perform lookup
    print(f"\nLooking up: {app_name}")

    # Step 1: Normalize name
    normalized = normalize_app_name(app_name)
    print(f"  Normalized: {normalized}")

    result = None
    matched_name = None
    match_method = None
    confidence = 0.0

    # Step 2: Try exact match WITH version first (if version provided)
    if version:
        search_with_version = f"{normalized} {version}"
        print(f"  Trying with version: {search_with_version}")
        results = query_nvd_cpe(search_with_version, max_results=5)

        if results:
            result = results[0]
            matched_name = search_with_version
            match_method = 'exact'
            confidence = 0.95
            print(f"  Found with version!")
        else:
            # No results with version, try without
            print(f"  No results with version, trying without: {normalized}")
            results = query_nvd_cpe(normalized, max_results=5)
            if results:
                # Found generic CPE - inject the actual version
                result = results[0]
                cpe_parts = result['cpe'].split(':')
                if len(cpe_parts) >= 6 and version:
                    cpe_parts[5] = version  # Replace version field
                    result['cpe'] = ':'.join(cpe_parts)
                matched_name = normalized
                match_method = 'exact_version_injected'
                confidence = 0.85
                print(f"  Found without version, injected version")
    else:
        # No version provided, search without it
        print(f"  No version provided, searching: {normalized}")
        results = query_nvd_cpe(normalized, max_results=5)
        if results:
            result = results[0]
            matched_name = normalized
            match_method = 'exact'
            confidence = 0.9
            print(f"  Found")

    # Step 3: If still no result, try backoff search
    if not result:
        print(f"  No exact match, trying backoff search...")
        result, matched_name = backoff_search(normalized)
        if result:
            match_method = 'backoff'
            confidence = 0.7
            # Inject version into backoff result if available
            if version:
                cpe_parts = result['cpe'].split(':')
                if len(cpe_parts) >= 6:
                    cpe_parts[5] = version
                    result['cpe'] = ':'.join(cpe_parts)
                    match_method = 'backoff_version_injected'

    # Step 4: If still no result, try LLM as last resort
    if not result:
        print("  Trying LLM fallback...")
        result = llm_cpe_lookup(app_name, publisher)
        matched_name = None
        if result:
            match_method = 'llm'
            confidence = 0.5

    # Save to database (even if no result found)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO cpe_mappings
            (original_name, normalized_name, matched_name, publisher, version, cpe, vendor, product, match_method, confidence_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            app_name,
            normalized,
            matched_name,
            publisher,
            version,
            result['cpe'] if result else None,
            result['vendor'] if result else None,
            result['product'] if result else None,
            match_method if result else 'not_found',
            confidence if result else 0.0
        ))
        conn.commit()

    if result:
        print(f"  Found: {result['cpe']}")
        return {
            'cpe': result['cpe'],
            'vendor': result['vendor'],
            'product': result['product'],
            'match_method': match_method,
            'matched_name': matched_name,
            'cached': False
        }
    else:
        print(f"  Not found")
        return {
            'cpe': None,
            'vendor': None,
            'product': None,
            'match_method': 'not_found',
            'cached': False
        }

# API Endpoints

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat()})

@app.route('/api/lookup', methods=['POST'])
def api_lookup():
    """Lookup CPE for a single application"""
    try:
        app_data = request.json
        result = lookup_cpe(app_data)

        return jsonify({
            'success': True,
            'app_name': app_data.get('Name'),
            'result': result
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/batch', methods=['POST'])
def api_batch():
    """Batch lookup CPEs for multiple applications"""
    try:
        apps = request.json

        if not isinstance(apps, list):
            return jsonify({
                'success': False,
                'error': 'Request body must be an array of applications'
            }), 400

        results = []
        for app_data in apps:
            result = lookup_cpe(app_data)
            results.append({
                'app_name': app_data.get('Name'),
                'publisher': app_data.get('Publisher'),
                'version': app_data.get('Version'),
                'result': result
            })

        return jsonify({
            'success': True,
            'total': len(results),
            'results': results
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/manual', methods=['POST'])
def api_manual():
    """Manually add or update CPE mapping"""
    try:
        data = request.json

        required_fields = ['Name', 'cpe']
        if not all(field in data for field in required_fields):
            return jsonify({
                'success': False,
                'error': f'Missing required fields: {required_fields}'
            }), 400

        # Extract vendor and product from CPE
        cpe = data['cpe']
        parts = cpe.split(':')
        vendor = parts[3] if len(parts) > 3 else None
        product = parts[4] if len(parts) > 4 else None

        with get_db() as conn:
            cursor = conn.cursor()

            # Check if exists
            cursor.execute('SELECT id FROM cpe_mappings WHERE original_name = ?', (data['Name'],))
            existing = cursor.fetchone()

            if existing:
                # Update existing
                cursor.execute('''
                    UPDATE cpe_mappings
                    SET cpe = ?, vendor = ?, product = ?, match_method = 'manual',
                        confidence_score = 1.0, last_verified = CURRENT_TIMESTAMP,
                        notes = ?, publisher = ?, version = ?
                    WHERE original_name = ?
                ''', (
                    cpe,
                    vendor,
                    product,
                    data.get('notes'),
                    data.get('Publisher'),
                    data.get('Version'),
                    data['Name']
                ))
                action = 'updated'
            else:
                # Insert new
                cursor.execute('''
                    INSERT INTO cpe_mappings
                    (original_name, normalized_name, matched_name, publisher, version, cpe, vendor, product, match_method, confidence_score, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual', 1.0, ?)
                ''', (
                    data['Name'],
                    normalize_app_name(data['Name']),
                    data['Name'],
                    data.get('Publisher'),
                    data.get('Version'),
                    cpe,
                    vendor,
                    product,
                    data.get('notes')
                ))
                action = 'created'

            conn.commit()

        return jsonify({
            'success': True,
            'action': action,
            'app_name': data['Name'],
            'cpe': cpe
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/stats', methods=['GET'])
def api_stats():
    """Get database statistics"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Total mappings
            cursor.execute('SELECT COUNT(*) as count FROM cpe_mappings')
            total = cursor.fetchone()['count']

            # By match method
            cursor.execute('''
                SELECT match_method, COUNT(*) as count
                FROM cpe_mappings
                GROUP BY match_method
            ''')
            by_method = {row['match_method']: row['count'] for row in cursor.fetchall()}

            # Success rate
            cursor.execute('SELECT COUNT(*) as count FROM cpe_mappings WHERE cpe IS NOT NULL')
            found = cursor.fetchone()['count']
            success_rate = (found / total * 100) if total > 0 else 0

            # Most queried
            cursor.execute('''
                SELECT original_name, cpe, times_queried
                FROM cpe_mappings
                WHERE cpe IS NOT NULL
                ORDER BY times_queried DESC
                LIMIT 10
            ''')
            most_queried = [dict(row) for row in cursor.fetchall()]

            return jsonify({
                'success': True,
                'stats': {
                    'total_mappings': total,
                    'found': found,
                    'not_found': total - found,
                    'success_rate': round(success_rate, 2),
                    'by_method': by_method,
                    'most_queried': most_queried
                }
            })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/search', methods=['GET'])
def api_search():
    """Search existing mappings"""
    try:
        query = request.args.get('q', '')

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM cpe_mappings
                WHERE original_name LIKE ? OR cpe LIKE ?
                ORDER BY times_queried DESC
                LIMIT 50
            ''', (f'%{query}%', f'%{query}%'))

            results = [dict(row) for row in cursor.fetchall()]

        return jsonify({
            'success': True,
            'query': query,
            'count': len(results),
            'results': results
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

if __name__ == '__main__':
    # Run Flask app
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
