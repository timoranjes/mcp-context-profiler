
#!/usr/bin/env python3
"""License check helper — validates a TIMO_ license key against Polar (public endpoint, no auth)."""
import json, sys, urllib.request

ORG_ID = "307df5be-71b4-4ae8-bb83-4418f807480c"
ENDPOINT = "https://api.polar.sh/v1/customer-portal/license-keys/validate"

def validate(key: str) -> dict:
    body = json.dumps({"key": key, "organization_id": ORG_ID}).encode()
    req = urllib.request.Request(ENDPOINT, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return {"valid": True, **json.loads(r.read().decode())}
    except Exception as e:
        return {"valid": False, "error": str(e)}

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: check_license.py TIMO_XXXX-XXXX-..."); sys.exit(1)
    result = validate(sys.argv[1].strip())
    print(json.dumps(result, indent=1))
    sys.exit(0 if result["valid"] else 1)
