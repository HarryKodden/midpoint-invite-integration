import json
import logging
import os
from collections.abc import Iterable

from authlib.integrations.flask_client import OAuth
from flask import Flask, redirect, render_template_string, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from group_lookup import (
    LOOKUP_BACKEND_META,
    LOOKUP_BACKENDS,
    GroupLookupError,
    build_group_lookup,
    resolve_user_key,
)

logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"))
logger = logging.getLogger(__name__)


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def normalize_groups(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def build_oidc_claims_param(raw: str | None) -> str | None:
    """Build the OIDC ``claims`` authorize-request parameter.

    Accepts either:
    - a full JSON object (passed through), or
    - a comma-separated list of claim names requested from userinfo
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    if text.startswith("{"):
        json.loads(text)
        return text
    names = split_csv(text)
    if not names:
        return None
    return json.dumps({"userinfo": {name: None for name in names}})


app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-me")
app.config["SESSION_COOKIE_SECURE"] = os.environ.get(
    "SESSION_COOKIE_SECURE", "false"
).lower() in {"1", "true", "yes"}
app.config["SESSION_COOKIE_SAMESITE"] = os.environ.get(
    "SESSION_COOKIE_SAMESITE", "Lax"
)

oidc_issuer_url = os.environ.get("OIDC_ISSUER_URL", "").rstrip("/")
oidc_client_id = os.environ.get("OIDC_CLIENT_ID", "")
oidc_client_secret = os.environ.get("OIDC_CLIENT_SECRET", "")
oidc_redirect_uri = os.environ.get("OIDC_REDIRECT_URI", "")
oidc_scope = os.environ.get("OIDC_SCOPE", "openid profile email groups")
oidc_claims_param = build_oidc_claims_param(os.environ.get("OIDC_CLAIMS"))
user_lookup_claims = split_csv(
    os.environ.get(
        "USER_LOOKUP_CLAIMS",
        "preferred_username,email,sub",
    )
)

oauth = OAuth(app)
oidc_ready = bool(oidc_issuer_url and oidc_client_id)

if oidc_ready:
    oauth.register(
        name="oidc",
        client_id=oidc_client_id,
        client_secret=oidc_client_secret,
        server_metadata_url=f"{oidc_issuer_url}/.well-known/openid-configuration",
        client_kwargs={"scope": oidc_scope},
    )

lookup_clients: dict[str, object] = {}
lookup_init_errors: dict[str, str] = {}
for backend_name in LOOKUP_BACKENDS:
    try:
        lookup_clients[backend_name] = build_group_lookup(backend_name)
    except Exception as exc:  # noqa: BLE001 - surface misconfig in /health
        lookup_init_errors[backend_name] = str(exc)
        logger.exception("Failed to initialize %s lookup backend", backend_name)


def is_authorized(user_groups: list[str], selected_groups: list[str]) -> bool:
    if not selected_groups:
        return False
    return bool(set(user_groups).intersection(selected_groups))


def matching_groups(user_groups: list[str], selected_groups: list[str]) -> list[str]:
    return sorted(set(user_groups).intersection(selected_groups))


def require_oidc() -> None:
    if oidc_ready:
        return
    raise RuntimeError(
        "OIDC is not configured. Set OIDC_ISSUER_URL and OIDC_CLIENT_ID."
    )


def selected_lookup_backend() -> str | None:
    backend = session.get("lookup_backend")
    if backend in LOOKUP_BACKENDS:
        return backend
    return None


def get_lookup_client(backend: str | None):
    if not backend:
        return None, "No lookup method selected"
    client = lookup_clients.get(backend)
    if client is None:
        return None, lookup_init_errors.get(backend) or f"{backend} backend not configured"
    return client, None


def load_available_groups(backend: str) -> tuple[list[str], str | None]:
    client, error = get_lookup_client(backend)
    if client is None:
        return [], error
    try:
        return client.list_groups(), None
    except GroupLookupError as exc:
        logger.warning("Failed to list groups via %s: %s", backend, exc)
        return [], str(exc)


def lookup_user_groups(
    userinfo: dict, backend: str
) -> tuple[list[str], str | None, str | None]:
    client, error = get_lookup_client(backend)
    if client is None:
        return [], None, error

    user_key = resolve_user_key(userinfo, user_lookup_claims)
    if not user_key:
        return [], None, (
            "No usable user key in token; checked claims: "
            + ", ".join(user_lookup_claims)
        )

    try:
        groups = client.lookup_groups(user_key)
        return groups, user_key, None
    except GroupLookupError as exc:
        logger.warning("Group lookup failed for %s via %s: %s", user_key, backend, exc)
        return [], user_key, str(exc)


TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Application — membership pull</title>
    <style>
      :root {
        color-scheme: light;
        --bg: #f6f3eb;
        --text: #1d2433;
        --muted: #5d687a;
        --accent: #2457d6;
        --good: #12795b;
        --bad: #b42318;
        --border: #d9d2c3;
      }
      body {
        margin: 0;
        font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: radial-gradient(circle at top, #fff8e7 0, var(--bg) 35%, #eef2f8 100%);
        color: var(--text);
      }
      main { max-width: 860px; margin: 64px auto; padding: 0 24px; }
      .card {
        background: rgba(255, 253, 247, 0.95);
        border: 1px solid var(--border);
        border-radius: 20px;
        padding: 28px;
        box-shadow: 0 18px 60px rgba(36, 38, 51, 0.12);
      }
      .badge {
        display: inline-block;
        padding: 6px 12px;
        border-radius: 999px;
        font-size: 12px;
        font-weight: 700;
        letter-spacing: 0.04em;
        text-transform: uppercase;
      }
      .good { background: rgba(18, 121, 91, 0.12); color: var(--good); }
      .bad { background: rgba(180, 35, 24, 0.12); color: var(--bad); }
      .neutral { background: rgba(93, 104, 122, 0.12); color: var(--muted); }
      button.button, a.button {
        display: inline-block;
        margin-right: 12px;
        margin-top: 16px;
        padding: 12px 18px;
        border-radius: 12px;
        text-decoration: none;
        color: white;
        background: var(--accent);
        border: 0;
        font: inherit;
        cursor: pointer;
      }
      button.button.secondary, a.button.secondary {
        background: #5d687a;
      }
      button.button:disabled {
        background: #9aa3b5;
        cursor: not-allowed;
      }
      dl {
        display: grid;
        grid-template-columns: 180px 1fr;
        gap: 10px 16px;
        margin: 24px 0 0;
      }
      dt { color: var(--muted); font-weight: 600; }
      dd { margin: 0; }
      ul { margin: 8px 0 0 20px; }
      .hint { color: var(--muted); }
      .method-list, .group-list {
        list-style: none;
        margin: 16px 0 0;
        padding: 0;
        border: 1px solid var(--border);
        border-radius: 12px;
      }
      .group-list {
        max-height: 320px;
        overflow: auto;
      }
      .method-list li, .group-list li {
        padding: 10px 14px;
        border-bottom: 1px solid var(--border);
      }
      .method-list li:last-child, .group-list li:last-child { border-bottom: 0; }
      .method-list label, .group-list label {
        display: flex;
        gap: 10px;
        align-items: flex-start;
        cursor: pointer;
      }
      .method-list .desc { color: var(--muted); font-size: 0.92em; margin-top: 2px; }
      .match { color: var(--good); font-weight: 600; }
      .steps { color: var(--muted); font-size: 0.92em; margin: 0 0 16px; }
    </style>
  </head>
  <body>
    <main>
      <section class="card">
        {% if user %}
          <span class="badge {{ 'good' if authorized else 'bad' }}">
            {{ 'authorized' if authorized else 'forbidden' }}
          </span>
        {% elif not lookup_backend %}
          <span class="badge neutral">select method</span>
        {% else %}
          <span class="badge neutral">select groups</span>
        {% endif %}
        <h1>Application — membership pull</h1>
        <p class="hint">
          Comparison demo: after OIDC login this app looks up memberships via the
          method you choose (MidPoint, SCIM, or LDAP). Access is granted when those
          memberships intersect the groups you select.
        </p>

        {% if not user %}
          {% if not lookup_backend %}
            <p class="steps">Step 1 of 3 — Choose lookup method</p>
            <form method="post" action="{{ url_for('select_method') }}" id="method-form">
              <h2>Lookup method</h2>
              <ul class="method-list">
                {% for name, meta in backend_meta.items() %}
                <li>
                  <label>
                    <input type="radio" name="backend" value="{{ name }}"
                      {% if not backends[name].ready %}disabled{% endif %}>
                    <span>
                      <strong>{{ meta.label }}</strong>
                      <div class="desc">
                        {{ meta.description }}
                        {% if not backends[name].ready %}
                          — unavailable: {{ backends[name].error }}
                        {% endif %}
                      </div>
                    </span>
                  </label>
                </li>
                {% endfor %}
              </ul>
              <button class="button" type="submit" id="method-btn" disabled>
                Continue to groups
              </button>
            </form>
          {% else %}
            <p class="steps">
              Step 2 of 3 — Select groups via <strong>{{ lookup_backend }}</strong>
            </p>
            <form method="post" action="{{ url_for('prepare_login') }}" id="group-form">
              <h2>Groups ({{ lookup_backend }})</h2>
              {% if catalog_error %}
                <p class="hint">Could not load groups: {{ catalog_error }}</p>
              {% elif not available_groups %}
                <p class="hint">No groups found via {{ lookup_backend }}.</p>
              {% else %}
                <ul class="group-list">
                  {% for group in available_groups %}
                    <li>
                      <label>
                        <input type="checkbox" name="group" value="{{ group }}"
                          {% if group in selected_groups %}checked{% endif %}>
                        <span>{{ group }}</span>
                      </label>
                    </li>
                  {% endfor %}
                </ul>
              {% endif %}
              <button class="button" type="submit" id="login-btn" disabled>
                Login with OIDC
              </button>
              <a class="button secondary" href="{{ url_for('reset_method') }}">
                Change method
              </a>
            </form>
          {% endif %}
        {% else %}
          <p class="steps">Step 3 of 3 — Authorization result</p>
          <p>Signed in as <strong>{{ user.name }}</strong>.</p>
          <a class="button" href="{{ url_for('logout') }}">Logout / try again</a>
          <dl>
            <dt>Issuer</dt>
            <dd>{{ issuer or 'not configured' }}</dd>
            <dt>Group lookup</dt>
            <dd>{{ lookup_backend }}</dd>
            <dt>Lookup key</dt>
            <dd>{{ user_lookup_key or 'n/a' }}</dd>
            <dt>Selected groups</dt>
            <dd>
              {% if selected_groups %}
                <ul>{% for group in selected_groups %}<li>{{ group }}</li>{% endfor %}</ul>
              {% else %}
                <span class="hint">None selected.</span>
              {% endif %}
            </dd>
            <dt>User groups</dt>
            <dd>
              {% if user_groups %}
                <ul>{% for group in user_groups %}<li>{{ group }}</li>{% endfor %}</ul>
              {% else %}
                <span class="hint">No groups found for this user.</span>
              {% endif %}
            </dd>
            <dt>Matching groups</dt>
            <dd>
              {% if matched_groups %}
                <ul>{% for group in matched_groups %}<li class="match">{{ group }}</li>{% endfor %}</ul>
              {% else %}
                <span class="hint">No overlap between selected groups and user memberships.</span>
              {% endif %}
            </dd>
            {% if lookup_error %}
            <dt>Lookup error</dt>
            <dd class="hint">{{ lookup_error }}</dd>
            {% endif %}
          </dl>
        {% endif %}
      </section>
    </main>
    {% if not user and not lookup_backend %}
    <script>
      (function () {
        const form = document.getElementById('method-form');
        const button = document.getElementById('method-btn');
        if (!form || !button) return;
        const sync = () => {
          button.disabled = !form.querySelector('input[name="backend"]:checked');
        };
        form.addEventListener('change', sync);
        sync();
      })();
    </script>
    {% elif not user and lookup_backend %}
    <script>
      (function () {
        const form = document.getElementById('group-form');
        const button = document.getElementById('login-btn');
        if (!form || !button) return;
        const sync = () => {
          const checked = form.querySelectorAll('input[name="group"]:checked').length;
          button.disabled = checked === 0;
        };
        form.addEventListener('change', sync);
        sync();
      })();
    </script>
    {% endif %}
  </body>
</html>
"""


def backend_status() -> dict:
    return {
        name: {
            "ready": name in lookup_clients,
            "error": lookup_init_errors.get(name),
        }
        for name in LOOKUP_BACKENDS
    }


@app.get("/")
def index():
    user = session.get("user")
    lookup_backend = selected_lookup_backend()
    selected_groups = session.get("selected_groups", [])
    user_groups = session.get("user_groups", [])
    matched = matching_groups(user_groups, selected_groups)
    authorized = bool(session.get("authorized"))
    available_groups, catalog_error = ([], None)
    if not user and lookup_backend:
        available_groups, catalog_error = load_available_groups(lookup_backend)

    status_code = 200
    if user and not authorized:
        status_code = 403

    return render_template_string(
        TEMPLATE,
        user=user,
        authorized=authorized,
        user_groups=user_groups,
        selected_groups=selected_groups,
        matched_groups=matched,
        available_groups=available_groups,
        catalog_error=catalog_error,
        issuer=oidc_issuer_url,
        lookup_backend=lookup_backend,
        backends=backend_status(),
        backend_meta=LOOKUP_BACKEND_META,
        user_lookup_key=session.get("user_lookup_key"),
        lookup_error=session.get("lookup_error"),
    ), status_code


@app.post("/select-method")
def select_method():
    backend = (request.form.get("backend") or "").strip().lower()
    if backend not in LOOKUP_BACKENDS or backend not in lookup_clients:
        return redirect(url_for("index"))
    session["lookup_backend"] = backend
    session.pop("selected_groups", None)
    session.pop("user", None)
    session.pop("user_groups", None)
    session.pop("authorized", None)
    session.pop("lookup_error", None)
    session.pop("user_lookup_key", None)
    return redirect(url_for("index"))


@app.get("/reset-method")
def reset_method():
    session.pop("lookup_backend", None)
    session.pop("selected_groups", None)
    session.pop("user", None)
    session.pop("user_groups", None)
    session.pop("authorized", None)
    session.pop("lookup_error", None)
    session.pop("user_lookup_key", None)
    return redirect(url_for("index"))


@app.post("/prepare-login")
def prepare_login():
    if not selected_lookup_backend():
        return redirect(url_for("index"))
    selected = request.form.getlist("group")
    if not selected:
        return redirect(url_for("index"))
    session["selected_groups"] = selected
    session.pop("user", None)
    session.pop("user_groups", None)
    session.pop("authorized", None)
    session.pop("lookup_error", None)
    session.pop("user_lookup_key", None)
    return redirect(url_for("login"))


@app.get("/login")
def login():
    require_oidc()
    if not selected_lookup_backend() or not session.get("selected_groups"):
        return redirect(url_for("index"))
    redirect_uri = oidc_redirect_uri or url_for("authorize", _external=True)
    kwargs = {}
    if oidc_claims_param:
        kwargs["claims"] = oidc_claims_param
    return oauth.oidc.authorize_redirect(redirect_uri, **kwargs)


@app.get("/auth/callback")
def authorize():
    require_oidc()
    # This issuer does not include "nonce" in the ID token (and does not list it
    # in claims_supported). Authlib's authorize_access_token() would reject that,
    # so exchange the code ourselves and load claims from the UserInfo endpoint.
    client = oauth.oidc
    if request.args.get("error"):
        raise RuntimeError(
            f"OIDC error: {request.args.get('error')}: "
            f"{request.args.get('error_description')}"
        )

    params = {
        "code": request.args["code"],
        "state": request.args.get("state"),
    }
    state_data = client.framework.get_state_data(session, params.get("state"))
    client.framework.clear_state_data(session, params.get("state"))
    params = client._format_state_params(state_data, params)
    token = client.fetch_access_token(**params)

    userinfo = client.userinfo(token=token)
    logger.info("Userinfo: %s", userinfo)

    backend = selected_lookup_backend() or "midpoint"
    selected_groups = session.get("selected_groups", [])
    user_groups, user_key, lookup_error = lookup_user_groups(userinfo, backend)
    matched = matching_groups(user_groups, selected_groups)

    session["user"] = {
        "name": userinfo.get("name")
        or userinfo.get("preferred_username")
        or userinfo.get("email")
        or userinfo.get("sub"),
        "email": userinfo.get("email"),
    }
    session["user_lookup_key"] = user_key
    session["user_groups"] = user_groups
    session["lookup_error"] = lookup_error
    session["authorized"] = (not lookup_error) and is_authorized(
        user_groups, selected_groups
    )
    logger.info(
        "Authz backend=%s selected=%s user_groups=%s matched=%s authorized=%s",
        backend,
        selected_groups,
        user_groups,
        matched,
        session["authorized"],
    )
    return redirect(url_for("index"))


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.get("/health")
def health():
    backends = backend_status()
    any_ready = any(info["ready"] for info in backends.values())
    return {
        "status": "ok" if oidc_ready and any_ready else "degraded",
        "oidc_ready": oidc_ready,
        "lookup_backends": backends,
        "user_lookup_claims": user_lookup_claims,
        "oidc_scope": oidc_scope,
        "oidc_claims": oidc_claims_param,
    }
