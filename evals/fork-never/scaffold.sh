#!/usr/bin/env bash
# Stage a small fake project in the eval's empty workspace so the routing decision has something to act on: a git
# checkout of acme/api on branch feature/token-refresh with a local "origin", a GitLab CI file, a Terraform folder
# with a saved plan for pre, a test suite, one untracked new file (app/refunds.py) and a 3,000-line failed job log.
# `claude plugin eval` runs it only with --scaffold. Every case that needs it holds an identical copy (a test checks).
set -eu
export GIT_AUTHOR_NAME=eval GIT_AUTHOR_EMAIL=eval@example.com GIT_COMMITTER_NAME=eval GIT_COMMITTER_EMAIL=eval@example.com
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
git init -q .
git checkout -q -b main
mkdir -p app tests infra hooks
cat > README.md <<'EOF'
# acme/api

Payment API of Acme. Merge requests and CI run on GitLab (project `acme/api`; `glab` is configured), tickets live in
the Jira project TT, and deployments go to the `pre` and `pro` environments with Terraform from `infra/`, where the
service runs as an Azure web app (`acme-api-pre`, `acme-api-pro`).
EOF
cat > .gitlab-ci.yml <<'EOF'
stages: [test, deploy]
test:
  stage: test
  script:
    - pip install -r requirements.txt
    - pytest -q
deploy-pre:
  stage: deploy
  script:
    - terraform -chdir=infra apply -auto-approve pre.tfplan
  environment: pre
EOF
echo "requests==2.32.3" > requirements.txt
: > app/__init__.py
cat > app/auth.py <<'EOF'
"""Token validation and refresh for the payment API."""
import time

import requests

REFRESH_MARGIN = 60  # seconds before expiry at which a token is refreshed


class TokenStore:
    """Holds the current access token and refreshes it before it expires."""

    def __init__(self, url, client_id, client_secret):
        self.url = url
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = None
        self.expires_at = 0.0
        self.session = None

    def valid(self):
        """True while the token has more than REFRESH_MARGIN seconds left."""
        return self.token is not None and time.time() < self.expires_at - REFRESH_MARGIN

    def get(self):
        """The access token, refreshed when it is missing or about to expire."""
        if not self.valid():
            self.refresh()
        return self.token

    def refresh(self):
        """Ask the identity provider for a new token with the client credentials."""
        response = self.client().post(
            self.url,
            data={"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret},
            timeout=10,
        )
        response.raise_for_status()
        body = response.json()
        self.token = body["access_token"]
        self.expires_at = time.time() + float(body.get("expires_in", 300))

    def client(self):
        if self.session is None:
            self.session = requests.Session()
        return self.session


def session_user(store, session):
    """The user record of a session, or None when the session has no user."""
    if session is None:
        return None
    headers = {"Authorization": "Bearer " + store.get()}
    response = store.client().get(session["url"], headers=headers, timeout=10)
    return response.json().get("user")
EOF
cat > app/client.py <<'EOF'
"""HTTP client for the payment provider."""
import requests

BASE = "https://pay.example.com/v1"


def charge(token, amount, currency):
    r = requests.post(BASE + "/charges", json={"amount": amount, "currency": currency},
                      headers={"Authorization": "Bearer " + token}, timeout=10)
    r.raise_for_status()
    return r.json()
EOF
cat > tests/test_auth.py <<'EOF'
import time

from app.auth import REFRESH_MARGIN, TokenStore


def test_valid_is_false_without_a_token():
    assert not TokenStore("https://idp.example.com/token", "id", "secret").valid()


def test_valid_is_true_with_a_fresh_token():
    store = TokenStore("https://idp.example.com/token", "id", "secret")
    store.token = "abc"
    store.expires_at = time.time() + REFRESH_MARGIN + 10
    assert store.valid()
EOF
cat > hooks/ops-nudge.py <<'EOF'
"""Nudge the session after a run of commands."""
import os

NUDGE_AT = 3  # calls at which the first reminder fires


def nudge_at():
    try:
        return int(os.environ.get("NUDGE_AT", str(NUDGE_AT)))
    except ValueError:
        return NUDGE_AT
EOF
cat > infra/main.tf <<'EOF'
variable "environment" {}
resource "azurerm_linux_web_app" "api" {
  name = "acme-api-${var.environment}"
}
EOF
echo 'environment = "pre"' > infra/pre.tfvars
echo 'environment = "pro"' > infra/pro.tfvars
echo "saved terraform plan for pre (binary in real life)" > infra/pre.tfplan
awk 'BEGIN { for (i = 1; i <= 3000; i++) {
  if (i == 2731) print "[02731] ERROR collecting tests/test_auth.py";
  else if (i == 2732) print "[02732] ImportError while importing test module tests/test_auth.py";
  else if (i == 2733) print "[02733] ModuleNotFoundError: No module named \"requests\"";
  else if (i == 2734) print "[02734] Hint: requirements.txt was not installed because pip cache mount failed";
  else if (i == 3000) print "[03000] ERROR: Job failed: exit code 1";
  else printf "[%05d] INFO pytest collection step %d ok\n", i, i } }' > job-1234.log
printf '.origin.git/\n__pycache__/\n' > .gitignore
git add -A
git commit -q -m "Initial import"
git init -q --bare .origin.git
git remote add origin "$PWD/.origin.git"
git push -q -u origin main
git checkout -q -b feature/token-refresh
printf '\n\ndef refresh_soon(store):\n    """True when the token will be refreshed on the next get()."""\n    return not store.valid()\n' >> app/auth.py
git commit -qam "Refresh tokens before expiry"
cat > app/refunds.py <<'EOF'
"""Refunds against the payment provider (new, not yet committed)."""
import requests

from app.client import BASE


def refund(token, charge_id):
    r = requests.post(BASE + "/charges/" + charge_id + "/refund", headers={"Authorization": "Bearer " + token}, timeout=10)
    r.raise_for_status()
    return r.json()
EOF
