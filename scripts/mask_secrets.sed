# Redact DB credentials from any piped output (test tracebacks, logs).
# Two independent maskers — a leak can take either shape:
#   1. URL form        postgresql://user:pass@host  ->  ://REDACTED@host
#   2. asyncpg repr    password='pass'  (ConnectionParameters in a traceback;
#      NOT covered by the URL masker — this is the gap that leaked AVLc7...)
# Usage: ... 2>&1 | sed -E -f scripts/mask_secrets.sed
s#://[^@]*@#://REDACTED@#g
s#(password=)'[^']*'#\1'REDACTED'#g
s#(password=)"[^"]*"#\1"REDACTED"#g
