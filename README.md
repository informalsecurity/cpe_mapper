# cpe_mapper
Dockerized api that takes Application Name and Version and works with NIST NVD API to generate a sort of accurate database of CPE's


# CPE Mapping Service

Flask API for automatic CPE (Common Platform Enumeration) mapping of installed Windows software using NVD database with intelligent backoff and LLM fallback.

## Features

- **NVD Integration**: Queries official NIST NVD database for CPE matches
- **Intelligent Backoff**: Automatically removes words from right to find matches
- **LLM Fallback**: Uses Claude API when NVD search fails
- **Local Caching**: SQLite database stores all mappings to avoid duplicate lookups
- **Batch Processing**: Handle multiple applications in single request
- **Manual Override**: Add/edit CPE mappings manually via API

## Quick Start

### 1. Deploy with Docker Compose

```bash
# Clone or copy files to server
cd /opt/cpe-mapper

# Optional: Add API keys for better performance
cp .env.example .env
nano .env

# Build and start
docker-compose up -d

# Check status
docker-compose logs -f
```

### 2. Verify Deployment

```bash
curl http://localhost:5000/health
```

## API Endpoints

### Health Check
```bash
GET /health
```

### Single Lookup
```bash
POST /api/lookup
Content-Type: application/json

{
    "Name": "7-Zip 24.09 (x64)",
    "Publisher": "Igor Pavlov",
    "Version": "24.09",
    "Source": "Registry"
}
```

### Batch Lookup
```bash
POST /api/batch
Content-Type: application/json

[
    {
        "InstallDate": null,
        "Publisher": "Igor Pavlov",
        "RegistryPath": "...",
        "CPE": "cpe:2.3:a:igor_pavlov:7_zip_24_09__x64_:24.09",
        "Name": "7-Zip 24.09 (x64)",
        "Source": "Registry",
        "Version": "24.09",
        "UninstallString": "...",
        "InstallLocation": "C:\\Program Files\\7-Zip\\"
    },
    {
        "Name": "Mozilla Firefox (x64 en-US)",
        "Publisher": "Mozilla",
        "Version": "144.0.2"
    }
]
```

### Manual Add/Update
```bash
POST /api/manual
Content-Type: application/json

{
    "Name": "7-Zip 24.09 (x64)",
    "Publisher": "Igor Pavlov",
    "Version": "24.09",
    "cpe": "cpe:2.3:a:7-zip:7-zip:24.09:*:*:*:*:*:*:*",
    "notes": "Manually verified from NVD"
}
```

### Search Mappings
```bash
GET /api/search?q=firefox
```

### Statistics
```bash
GET /api/stats
```

## PowerShell Integration

```powershell
# Single lookup
$app = @{
    Name = "7-Zip 24.09 (x64)"
    Publisher = "Igor Pavlov"
    Version = "24.09"
}

$result = Invoke-RestMethod -Uri "http://your-server:5000/api/lookup" `
    -Method Post `
    -ContentType "application/json" `
    -Body ($app | ConvertTo-Json)

Write-Host "CPE: $($result.result.cpe)"

# Batch lookup
$apps = Get-InstalledSoftware  # Your collection function
$appsJson = $apps | ConvertTo-Json -Depth 10

$results = Invoke-RestMethod -Uri "http://your-server:5000/api/batch" `
    -Method Post `
    -ContentType "application/json" `
    -Body $appsJson

$results.results | ForEach-Object {
    Write-Host "$($_.app_name) -> $($_.result.cpe)"
}

# Manual correction
$manual = @{
    Name = "Notepad++ (64-bit x64)"
    cpe = "cpe:2.3:a:notepad-plus-plus:notepad++:8.7.7:*:*:*:*:*:*:*"
    notes = "Corrected vendor format"
}

Invoke-RestMethod -Uri "http://your-server:5000/api/manual" `
    -Method Post `
    -ContentType "application/json" `
    -Body ($manual | ConvertTo-Json)
```

## How It Works

### Lookup Process

1. **Check Cache**: Query local SQLite database
   - If found: Return cached result immediately
   - If found as "not_found": Return NULL without re-querying NVD

2. **Normalize Name**: Remove common patterns
   - Trademark symbols: (R), (TM), ®, ™
   - Language codes: en-us, en-US
   - Architecture: (x64), (x86), (ARM64)
   - Version numbers at end
   - Edition names: Update, Runtime, Redistributable

3. **NVD Exact Search**: Query with normalized name
   - Returns: CPE, vendor, product
   - Match method: "exact"

4. **Backoff Search**: If no exact match
   - Remove words from right to left
   - Query NVD after each removal
   - Stop at first match
   - Match method: "backoff"

5. **LLM Fallback**: If NVD search fails (optional)
   - Send to Claude API with context
   - Get CPE recommendation
   - Match method: "llm"

6. **Save Result**: Store in database
   - Even if no match found (prevents re-querying)
   - Track: match method, confidence, query count

### Database Schema

```sql
CREATE TABLE cpe_mappings (
    id INTEGER PRIMARY KEY,
    original_name TEXT UNIQUE,      -- "7-Zip 24.09 (x64)"
    normalized_name TEXT,            -- "7-Zip"
    matched_name TEXT,               -- What search term found it
    publisher TEXT,                  -- "Igor Pavlov"
    version TEXT,                    -- "24.09"
    cpe TEXT,                        -- "cpe:2.3:a:7-zip:7-zip:24.09:*:*:*:*:*:*:*"
    vendor TEXT,                     -- "7-zip"
    product TEXT,                    -- "7-zip"
    match_method TEXT,               -- "exact", "backoff", "llm", "manual", "not_found"
    confidence_score REAL,           -- 0.0 to 1.0
    date_added TIMESTAMP,
    last_verified TIMESTAMP,
    times_queried INTEGER,           -- Usage tracking
    notes TEXT
);
```

## Configuration

### Environment Variables

- `DATABASE_PATH`: SQLite database location (default: `/data/cpe_mappings.db`)
- `NVD_API_KEY`: Optional NVD API key (increases rate limit 10x)
- `LLM_API_KEY`: Optional Anthropic API key for fallback
- `PORT`: API port (default: 5000)

### Rate Limiting

- **Without NVD API Key**: 5 requests per 30 seconds (6 second delay)
- **With NVD API Key**: 50 requests per 30 seconds (0.6 second delay)

Get NVD API key: https://nvd.nist.gov/developers/request-an-api-key

### Performance

- **Cached lookups**: < 10ms
- **NVD exact match**: ~6 seconds (rate limiting)
- **NVD backoff search**: ~6-30 seconds (multiple queries)
- **LLM fallback**: ~2-5 seconds

**Recommendation**: Pre-populate database with batch job, then use for real-time lookups

## Maintenance

### View Logs
```bash
docker-compose logs -f
```

### Backup Database
```bash
docker cp cpe-mapper:/data/cpe_mappings.db ./backup_$(date +%Y%m%d).db
```

### Restore Database
```bash
docker cp ./backup.db cpe-mapper:/data/cpe_mappings.db
docker-compose restart
```

### Update Application
```bash
# Pull new code
git pull

# Rebuild and restart
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

## Troubleshooting

### Rate Limiting Issues
- Add NVD API key to `.env`
- Check logs for 429 errors
- Increase `RATE_LIMIT_DELAY` if needed

### Database Locked
- Check for multiple instances
- Restart container: `docker-compose restart`

### LLM Not Working
- Verify `LLM_API_KEY` in `.env`
- Check Anthropic API quota
- Review logs for API errors

### No Results Found
- Check NVD is accessible: `curl https://services.nvd.nist.gov/rest/json/cpes/2.0`
- Verify normalization is working (check logs)
- Try manual entry: `POST /api/manual`

## Example Responses

### Successful Lookup
```json
{
  "success": true,
  "app_name": "7-Zip 24.09 (x64)",
  "result": {
    "cpe": "cpe:2.3:a:7-zip:7-zip:24.09:*:*:*:*:*:*:*",
    "vendor": "7-zip",
    "product": "7-zip",
    "match_method": "exact",
    "cached": false
  }
}
```

### Not Found (Cached)
```json
{
  "success": true,
  "app_name": "Some Unknown App",
  "result": {
    "cpe": null,
    "vendor": null,
    "product": null,
    "match_method": "not_found",
    "cached": true
  }
}
```

### Batch Results
```json
{
  "success": true,
  "total": 3,
  "results": [
    {
      "app_name": "7-Zip 24.09 (x64)",
      "publisher": "Igor Pavlov",
      "version": "24.09",
      "result": {
        "cpe": "cpe:2.3:a:7-zip:7-zip:24.09:*:*:*:*:*:*:*",
        "vendor": "7-zip",
        "product": "7-zip",
        "match_method": "exact",
        "cached": true
      }
    }
  ]
}
```

## License

Internal use only
