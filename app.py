import io
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from functools import wraps
from io import StringIO
from textwrap import wrap
from urllib.parse import urlparse

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from flask import Flask, g, jsonify, redirect, render_template, request, session, send_file, url_for
from flask_dance.contrib.github import github, make_github_blueprint
from flask_dance.contrib.google import google, make_google_blueprint
from dotenv import load_dotenv
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sklearn.cluster import KMeans
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(BASE_DIR, ".env"), override=False)

app = Flask(__name__)
app.secret_key = (
    os.getenv("SECRET_KEY")
    or os.getenv("FLASK_SECRET_KEY")
    or "replace-with-a-strong-secret-key"
)
APP_BASE_URL = (os.getenv("APP_BASE_URL") or "").strip().rstrip("/")
CANONICAL_OAUTH_HOST = ""
IS_RENDER = os.getenv("RENDER", "").lower() == "true"
IS_RAILWAY = bool(
    os.getenv("RAILWAY_ENVIRONMENT")
    or os.getenv("RAILWAY_PUBLIC_DOMAIN")
    or os.getenv("RAILWAY_STATIC_URL")
)
IS_PRODUCTION = (
    os.getenv("FLASK_ENV", "").lower() == "production"
    or os.getenv("ENVIRONMENT", "").lower() == "production"
    or IS_RENDER
    or IS_RAILWAY
)

if APP_BASE_URL and "://" not in APP_BASE_URL:
    APP_BASE_URL = f"https://{APP_BASE_URL}"

if not APP_BASE_URL:
    render_external_url = (os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    railway_static_url = (os.getenv("RAILWAY_STATIC_URL") or "").strip().rstrip("/")
    railway_public_domain = (os.getenv("RAILWAY_PUBLIC_DOMAIN") or "").strip()

    if render_external_url:
        APP_BASE_URL = render_external_url
    elif railway_static_url:
        APP_BASE_URL = railway_static_url
    elif railway_public_domain:
        APP_BASE_URL = f"https://{railway_public_domain}"

if IS_PRODUCTION and not APP_BASE_URL:
    app.logger.warning(
        "APP_BASE_URL is not set in production. OAuth callbacks will use the incoming request host; set APP_BASE_URL for a fixed canonical domain."
    )

if APP_BASE_URL:
    parsed_base_url = urlparse(APP_BASE_URL)
    if parsed_base_url.scheme != "https" or not parsed_base_url.netloc:
        app.logger.warning(
            "APP_BASE_URL should be a valid https URL (for example: https://your-app.onrender.com). Current value: %s",
            APP_BASE_URL,
        )
    if parsed_base_url.netloc:
        APP_BASE_URL = f"https://{parsed_base_url.netloc}"
        CANONICAL_OAUTH_HOST = parsed_base_url.netloc.lower()


def resolve_data_dir() -> str:
    configured_data_dir = (os.getenv("DATA_DIR") or "").strip()
    candidate_dirs = []

    if configured_data_dir:
        candidate_dirs.append(configured_data_dir)

    candidate_dirs.append(os.path.join(tempfile.gettempdir(), "ml_project"))
    candidate_dirs.append(BASE_DIR)

    for candidate in candidate_dirs:
        try:
            os.makedirs(candidate, exist_ok=True)
            write_test_path = os.path.join(candidate, ".write_test")
            with open(write_test_path, "w", encoding="utf-8") as handle:
                handle.write("ok")
            os.remove(write_test_path)

            if configured_data_dir and candidate != configured_data_dir:
                app.logger.warning(
                    "Configured DATA_DIR '%s' is not writable. Falling back to '%s'.",
                    configured_data_dir,
                    candidate,
                )

            return candidate
        except OSError:
            continue

    raise RuntimeError("Unable to find a writable DATA_DIR for runtime files.")

app.config["PREFERRED_URL_SCHEME"] = "https"
if CANONICAL_OAUTH_HOST:
    app.config["SERVER_NAME"] = CANONICAL_OAUTH_HOST
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

DATA_DIR = resolve_data_dir()
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
IMAGE_DIR = os.path.join(BASE_DIR, "static", "images")
DB_PATH = os.path.join(DATA_DIR, "app.db")
ALLOWED_EXTENSIONS = {"csv"}
FEATURE_COLUMNS = ["Annual Income (k$)", "Spending Score (1-100)"]
GOOGLE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]
GITHUB_SCOPES = "read:user,user:email"

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(IMAGE_DIR, exist_ok=True)

def get_provider_redirect_uri(provider: str) -> str:
    provider = provider.lower()
    if provider not in {"google", "github"}:
        raise ValueError("Unsupported OAuth provider.")

    callback_path = f"/login/{provider}/authorized"
    if APP_BASE_URL:
        return f"{APP_BASE_URL}{callback_path}"
    return callback_path


def get_canonical_oauth_host() -> str:
    return CANONICAL_OAUTH_HOST


def create_oauth_blueprints():
    google_client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
    google_client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
    github_client_id = os.getenv("GITHUB_OAUTH_CLIENT_ID")
    github_client_secret = os.getenv("GITHUB_OAUTH_CLIENT_SECRET")

    app.logger.info(
        "OAuth env status | GOOGLE_OAUTH_CLIENT_ID=%s GOOGLE_OAUTH_CLIENT_SECRET=%s GITHUB_OAUTH_CLIENT_ID=%s GITHUB_OAUTH_CLIENT_SECRET=%s",
        bool(google_client_id),
        bool(google_client_secret),
        bool(github_client_id),
        bool(github_client_secret),
    )

    providers = {
        "google": bool(google_client_id and google_client_secret),
        "github": bool(github_client_id and github_client_secret),
    }
    app.logger.info(
        "OAuth redirect URIs | google=%s github=%s",
        get_provider_redirect_uri("google"),
        get_provider_redirect_uri("github"),
    )
    if not APP_BASE_URL:
        app.logger.warning("APP_BASE_URL is not set. Set APP_BASE_URL in production to lock OAuth callback host.")

    missing_vars = {
        "google": [],
        "github": [],
    }

    if not google_client_id:
        missing_vars["google"].append("GOOGLE_OAUTH_CLIENT_ID")
    if not google_client_secret:
        missing_vars["google"].append("GOOGLE_OAUTH_CLIENT_SECRET")
    if not github_client_id:
        missing_vars["github"].append("GITHUB_OAUTH_CLIENT_ID")
    if not github_client_secret:
        missing_vars["github"].append("GITHUB_OAUTH_CLIENT_SECRET")

    if providers["google"]:
        google_bp = make_google_blueprint(
            client_id=google_client_id,
            client_secret=google_client_secret,
            scope=GOOGLE_SCOPES,
            login_url="/google",
            authorized_url="/google/authorized",
            redirect_to="auth_google",
        )
        app.register_blueprint(google_bp, url_prefix="/login")
    else:
        app.logger.warning("Google OAuth disabled. Missing: %s", ", ".join(missing_vars["google"]))

    if providers["github"]:
        github_bp = make_github_blueprint(
            client_id=github_client_id,
            client_secret=github_client_secret,
            scope=GITHUB_SCOPES,
            login_url="/github",
            authorized_url="/github/authorized",
            redirect_to="auth_github",
        )
        app.register_blueprint(github_bp, url_prefix="/login")
    else:
        app.logger.warning("GitHub OAuth disabled. Missing: %s", ", ".join(missing_vars["github"]))

    return providers, missing_vars


OAUTH_PROVIDERS, OAUTH_MISSING_VARS = create_oauth_blueprints()

if app.secret_key == "replace-with-a-strong-secret-key":
    app.logger.warning("Using default SECRET_KEY. Set SECRET_KEY in the environment for secure sessions.")


@app.after_request
def apply_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH) as connection:
        cursor = connection.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                provider_user_id TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                avatar_url TEXT,
                password_hash TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        existing_cols = [row[1] for row in cursor.execute("PRAGMA table_info(users)").fetchall()]
        if "password_hash" not in existing_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS uploaded_datasets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                original_filename TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                uploaded_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                selected_k INTEGER NOT NULL,
                optimal_k INTEGER NOT NULL,
                total_customers INTEGER NOT NULL,
                average_income REAL NOT NULL,
                average_spending_score REAL NOT NULL,
                cluster_details TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        connection.commit()


init_db()


def current_timestamp() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None

    db = get_db()
    row = db.execute(
        "SELECT id, email, name, avatar_url FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    return dict(row) if row else None


@app.before_request
def load_current_user():
    canonical_host = get_canonical_oauth_host()
    request_host = (request.host or "").lower()
    if canonical_host and request_host and request_host != canonical_host and request.method in {"GET", "HEAD"}:
        canonical_url = f"https://{canonical_host}{request.full_path.rstrip('?')}"
        app.logger.info("Redirecting non-canonical host to OAuth canonical host: %s", canonical_url)
        return redirect(canonical_url, code=302)

    g.user = get_current_user()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user:
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def api_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user:
            return jsonify({"error": "Authentication required."}), 401
        return view(*args, **kwargs)

    return wrapped


def upsert_user(provider: str, provider_user_id: str, email: str, name: str, avatar_url: str = ""):
    db = get_db()
    timestamp = current_timestamp()

    existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        db.execute(
            """
            UPDATE users
            SET provider = ?, provider_user_id = ?, name = ?, avatar_url = ?, updated_at = ?
            WHERE id = ?
            """,
            (provider, provider_user_id, name, avatar_url, timestamp, existing["id"]),
        )
        user_id = existing["id"]
    else:
        cursor = db.execute(
            """
            INSERT INTO users (provider, provider_user_id, email, name, avatar_url, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (provider, provider_user_id, email, name, avatar_url, timestamp, timestamp),
        )
        user_id = cursor.lastrowid

    db.commit()
    return user_id


def login_user(user_id: int):
    db = get_db()
    row = db.execute("SELECT id, email, name, avatar_url FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return
    session["user_id"] = row["id"]
    session["user_name"] = row["name"]
    session["user_email"] = row["email"]
    session["user_avatar"] = row["avatar_url"] or ""
    session.permanent = True


def create_local_user(name: str, email: str, password: str):
    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        return None, "An account with this email already exists."

    now = current_timestamp()
    password_hash = generate_password_hash(password)
    cursor = db.execute(
        """
        INSERT INTO users (provider, provider_user_id, email, name, avatar_url, password_hash, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("local", email, email, name, "", password_hash, now, now),
    )
    db.commit()
    return cursor.lastrowid, None


def authenticate_local_user(email: str, password: str):
    db = get_db()
    row = db.execute(
        "SELECT id, password_hash FROM users WHERE email = ? AND provider = 'local'",
        (email,),
    ).fetchone()
    if not row or not row["password_hash"]:
        return None
    if not check_password_hash(row["password_hash"], password):
        return None
    return row["id"]


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def load_dataset(file_path: str) -> pd.DataFrame:
    return pd.read_csv(file_path)


def extract_features(df: pd.DataFrame) -> pd.DataFrame:
    missing = [col for col in FEATURE_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(
            f"Dataset must include columns: {', '.join(FEATURE_COLUMNS)}. Missing: {', '.join(missing)}"
        )

    features = df[FEATURE_COLUMNS].copy()
    features = features.apply(pd.to_numeric, errors="coerce").dropna()

    if features.empty:
        raise ValueError("No valid rows found for selected features after cleaning missing values.")

    if len(features) < 3:
        raise ValueError("At least 3 valid rows are required to run K-Means clustering.")

    return features


def elbow_inertia_values(features: pd.DataFrame):
    max_k = min(10, len(features) - 1)
    if max_k < 2:
        raise ValueError("Not enough data points to estimate an optimal number of clusters.")

    k_values = list(range(1, max_k + 1))
    inertia_values = []

    for k in k_values:
        model = KMeans(n_clusters=k, random_state=42, n_init=10)
        model.fit(features)
        inertia_values.append(model.inertia_)

    return k_values, inertia_values


def choose_optimal_k(k_values, inertia_values) -> int:
    # Geometric elbow detection: choose point with max distance from line joining first and last inertia point.
    x1, y1 = k_values[0], inertia_values[0]
    x2, y2 = k_values[-1], inertia_values[-1]

    denominator = ((y2 - y1) ** 2 + (x2 - x1) ** 2) ** 0.5
    if denominator == 0:
        return 3 if len(k_values) >= 3 else k_values[0]

    distances = []
    for x0, y0 in zip(k_values, inertia_values):
        numerator = abs((y2 - y1) * x0 - (x2 - x1) * y0 + x2 * y1 - y2 * x1)
        distances.append(numerator / denominator)

    optimal_index = distances.index(max(distances))
    return k_values[optimal_index]


def run_kmeans(features: pd.DataFrame, n_clusters: int):
    model = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
    labels = model.fit_predict(features)
    centroids = model.cluster_centers_
    return labels, centroids


def save_elbow_plot(k_values, inertia_values, optimal_k: int) -> str:
    plt.figure(figsize=(7, 5))
    plt.plot(k_values, inertia_values, marker="o", linewidth=2, color="#1f77b4")
    plt.axvline(x=optimal_k, color="#e63946", linestyle="--", label=f"Optimal K = {optimal_k}")
    plt.title("Elbow Method")
    plt.xlabel("Number of Clusters (K)")
    plt.ylabel("Inertia")
    plt.legend()
    plt.tight_layout()

    filename = f"elbow_{datetime.now().strftime('%Y%m%d%H%M%S')}.png"
    path = os.path.join(IMAGE_DIR, filename)
    plt.savefig(path)
    plt.close()
    return filename


def save_cluster_plot(features: pd.DataFrame, labels, centroids) -> str:
    plt.figure(figsize=(8, 6))
    plt.scatter(
        features[FEATURE_COLUMNS[0]],
        features[FEATURE_COLUMNS[1]],
        c=labels,
        cmap="tab10",
        alpha=0.8,
        s=40,
        edgecolor="k",
        linewidth=0.2,
    )
    plt.scatter(
        centroids[:, 0],
        centroids[:, 1],
        c="red",
        s=220,
        marker="X",
        edgecolor="white",
        linewidth=1.0,
        label="Centroids",
    )
    plt.title("Customer Segments (K-Means)")
    plt.xlabel(FEATURE_COLUMNS[0])
    plt.ylabel(FEATURE_COLUMNS[1])
    plt.legend(loc="upper left")
    plt.tight_layout()

    filename = f"clusters_{datetime.now().strftime('%Y%m%d%H%M%S')}.png"
    path = os.path.join(IMAGE_DIR, filename)
    plt.savefig(path)
    plt.close()
    return filename


def interpret_clusters(centroids) -> list:
    interpretations = []
    incomes = centroids[:, 0]
    scores = centroids[:, 1]

    income_median = float(pd.Series(incomes).median())
    score_median = float(pd.Series(scores).median())

    for idx, (income, score) in enumerate(centroids):
        income_level = "High Income" if income >= income_median else "Low Income"
        score_level = "High Spending" if score >= score_median else "Low Spending"
        interpretations.append(f"Cluster {idx}: {income_level} - {score_level}")

    return interpretations


def process_dataset(file_path: str):
    df = load_dataset(file_path)
    features = extract_features(df)

    k_values, inertia_values = elbow_inertia_values(features)
    optimal_k = choose_optimal_k(k_values, inertia_values)

    labels, centroids = run_kmeans(features, optimal_k)

    elbow_plot = save_elbow_plot(k_values, inertia_values, optimal_k)
    cluster_plot = save_cluster_plot(features, labels, centroids)

    result_df = features.copy()
    result_df["Cluster"] = labels

    table_rows = result_df.reset_index(drop=True).head(120).to_dict(orient="records")
    cluster_summary = (
        result_df.groupby("Cluster")
        .size()
        .reset_index(name="Customers")
        .sort_values("Cluster")
        .to_dict(orient="records")
    )

    return {
        "optimal_k": optimal_k,
        "elbow_plot": elbow_plot,
        "cluster_plot": cluster_plot,
        "table_rows": table_rows,
        "total_rows": len(result_df),
        "cluster_summary": cluster_summary,
        "interpretations": interpret_clusters(centroids),
    }


def recommendation_for_segment(segment: str) -> str:
    recommendations = {
        "High Income - High Spending": "Target premium loyalty campaigns and exclusive bundles.",
        "High Income - Low Spending": "Use personalized offers to increase conversion from high-potential customers.",
        "Low Income - High Spending": "Promote value packs and time-limited deals to maintain frequency.",
        "Low Income - Low Spending": "Run awareness campaigns and entry-level promotions to activate this segment.",
    }
    return recommendations.get(segment, "Monitor this segment and tailor campaigns based on behavior trends.")


def build_dashboard_payload(file_path: str, selected_k: int = None):
    df = load_dataset(file_path)
    features = extract_features(df)

    k_values, inertia_values = elbow_inertia_values(features)
    optimal_k = choose_optimal_k(k_values, inertia_values)

    min_k = 2
    max_k = max(min(10, len(features) - 1), min_k)
    optimal_k = min(max(optimal_k, min_k), max_k)

    if selected_k is None:
        selected_k = optimal_k
    selected_k = int(selected_k)
    selected_k = min(max(selected_k, min_k), max_k)

    labels, centroids = run_kmeans(features, selected_k)

    result_df = features.copy().reset_index(drop=True)
    result_df["Cluster"] = labels

    avg_income = float(result_df[FEATURE_COLUMNS[0]].mean())
    avg_score = float(result_df[FEATURE_COLUMNS[1]].mean())

    centroid_income_series = pd.Series(centroids[:, 0])
    centroid_score_series = pd.Series(centroids[:, 1])
    income_median = float(centroid_income_series.median())
    score_median = float(centroid_score_series.median())

    cluster_details = []
    total_customers = len(result_df)
    for cluster_id in sorted(result_df["Cluster"].unique()):
        cluster_rows = result_df[result_df["Cluster"] == cluster_id]
        cluster_avg_income = float(cluster_rows[FEATURE_COLUMNS[0]].mean())
        cluster_avg_score = float(cluster_rows[FEATURE_COLUMNS[1]].mean())
        income_level = "High Income" if cluster_avg_income >= income_median else "Low Income"
        score_level = "High Spending" if cluster_avg_score >= score_median else "Low Spending"
        segment = f"{income_level} - {score_level}"

        cluster_details.append(
            {
                "cluster": int(cluster_id),
                "customers": int(len(cluster_rows)),
                "percentage": round((len(cluster_rows) / total_customers) * 100, 2),
                "avg_income": round(cluster_avg_income, 2),
                "avg_score": round(cluster_avg_score, 2),
                "segment": segment,
                "recommendation": recommendation_for_segment(segment),
            }
        )

    table_rows = result_df.copy()
    table_rows[FEATURE_COLUMNS[0]] = table_rows[FEATURE_COLUMNS[0]].round(2)
    table_rows[FEATURE_COLUMNS[1]] = table_rows[FEATURE_COLUMNS[1]].round(2)

    points = [
        {
            "income": float(row[FEATURE_COLUMNS[0]]),
            "score": float(row[FEATURE_COLUMNS[1]]),
            "cluster": int(row["Cluster"]),
            "customer_id": int(idx + 1),
        }
        for idx, row in table_rows.iterrows()
    ]

    centroid_points = [
        {
            "income": float(c[0]),
            "score": float(c[1]),
            "cluster": int(i),
        }
        for i, c in enumerate(centroids)
    ]

    return {
        "k_values": k_values,
        "inertia_values": [round(float(v), 2) for v in inertia_values],
        "optimal_k": int(optimal_k),
        "selected_k": int(selected_k),
        "k_range": {"min": min_k, "max": max_k},
        "analytics": {
            "total_customers": int(total_customers),
            "average_income": round(avg_income, 2),
            "average_spending_score": round(avg_score, 2),
            "optimal_k": int(optimal_k),
        },
        "cluster_details": cluster_details,
        "points": points,
        "centroids": centroid_points,
        "table_rows": table_rows.to_dict(orient="records"),
        "cluster_ids": sorted(int(v) for v in result_df["Cluster"].unique().tolist()),
    }


@app.route("/")
@login_required
def index():
    return render_template("index.html", user=g.user)


@app.route("/login")
def login():
    if g.user:
        return redirect(url_for("index"))

    mode = request.args.get("mode", "signup")
    success_message = request.args.get("success")
    show_signup = mode != "signin"

    missing_messages = []
    if not OAUTH_PROVIDERS["google"]:
        missing_messages.append(f"Google missing: {', '.join(OAUTH_MISSING_VARS['google'])}")
    if not OAUTH_PROVIDERS["github"]:
        missing_messages.append(f"GitHub missing: {', '.join(OAUTH_MISSING_VARS['github'])}")

    oauth_hint = "OAuth configured. Choose a provider to continue."
    if missing_messages:
        oauth_hint = " | ".join(missing_messages)

    return render_template(
        "login.html",
        google_enabled=OAUTH_PROVIDERS["google"],
        github_enabled=OAUTH_PROVIDERS["github"],
        oauth_hint=oauth_hint,
        oauth_missing=OAUTH_MISSING_VARS,
        show_signup=show_signup,
        success_message=success_message,
    )


@app.route("/login/local", methods=["POST"])
def login_local():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""

    if not email or not password:
        return render_template(
            "login.html",
            error="Email and password are required.",
            google_enabled=OAUTH_PROVIDERS["google"],
            github_enabled=OAUTH_PROVIDERS["github"],
            oauth_hint="Use OAuth or local account login.",
            oauth_missing=OAUTH_MISSING_VARS,
            show_signup=False,
        )

    user_id = authenticate_local_user(email, password)
    if not user_id:
        return render_template(
            "login.html",
            error="Invalid email or password.",
            google_enabled=OAUTH_PROVIDERS["google"],
            github_enabled=OAUTH_PROVIDERS["github"],
            oauth_hint="Use OAuth or local account login.",
            oauth_missing=OAUTH_MISSING_VARS,
            show_signup=False,
        )

    login_user(user_id)
    return redirect(url_for("index"))


@app.route("/signup", methods=["POST"])
def signup():
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    confirm_password = request.form.get("confirm_password") or ""

    if not name or not email or not password:
        return render_template(
            "login.html",
            error="Name, email, and password are required for sign up.",
            google_enabled=OAUTH_PROVIDERS["google"],
            github_enabled=OAUTH_PROVIDERS["github"],
            oauth_hint="Use OAuth or create a local account.",
            oauth_missing=OAUTH_MISSING_VARS,
            show_signup=True,
        )

    if len(password) < 8:
        return render_template(
            "login.html",
            error="Password must be at least 8 characters.",
            google_enabled=OAUTH_PROVIDERS["google"],
            github_enabled=OAUTH_PROVIDERS["github"],
            oauth_hint="Use OAuth or create a local account.",
            oauth_missing=OAUTH_MISSING_VARS,
            show_signup=True,
        )

    if password != confirm_password:
        return render_template(
            "login.html",
            error="Password and confirm password do not match.",
            google_enabled=OAUTH_PROVIDERS["google"],
            github_enabled=OAUTH_PROVIDERS["github"],
            oauth_hint="Use OAuth or create a local account.",
            oauth_missing=OAUTH_MISSING_VARS,
            show_signup=True,
        )

    user_id, error = create_local_user(name, email, password)
    if error:
        return render_template(
            "login.html",
            error=error,
            google_enabled=OAUTH_PROVIDERS["google"],
            github_enabled=OAUTH_PROVIDERS["github"],
            oauth_hint="Use OAuth or create a local account.",
            oauth_missing=OAUTH_MISSING_VARS,
            show_signup=True,
        )

    return redirect(
        url_for(
            "login",
            mode="signin",
            success="Account created successfully. Please sign in using Email, Google, or GitHub.",
        )
    )


@app.route("/auth/google")
def auth_google():
    if not OAUTH_PROVIDERS["google"]:
        return render_template("login.html", error="Google OAuth is not configured.", google_enabled=False, github_enabled=OAUTH_PROVIDERS["github"], oauth_hint="Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET." )

    if not google.authorized:
        oauth_callback_uri = url_for(
            "google.authorized",
            _external=True,
            _scheme=app.config.get("PREFERRED_URL_SCHEME", "https"),
        )
        provider_login_url = url_for(
            "google.login",
            _external=True,
            _scheme=app.config.get("PREFERRED_URL_SCHEME", "https"),
        )
        app.logger.info(
            "OAuth start | provider=google fixed_redirect_uri=%s generated_oauth_callback_uri=%s provider_login_url=%s request_host=%s",
            get_provider_redirect_uri("google"),
            oauth_callback_uri,
            provider_login_url,
            request.host,
        )
        return redirect(provider_login_url)

    response = google.get("/oauth2/v2/userinfo")
    if not response.ok:
        return render_template("login.html", error="Google authentication failed.", google_enabled=OAUTH_PROVIDERS["google"], github_enabled=OAUTH_PROVIDERS["github"], oauth_hint="Try again or check OAuth credentials.")

    payload = response.json()
    email = payload.get("email")
    if not email:
        return render_template("login.html", error="Google account email is required.", google_enabled=OAUTH_PROVIDERS["google"], github_enabled=OAUTH_PROVIDERS["github"], oauth_hint="Grant email permission and retry.")

    user_id = upsert_user(
        provider="google",
        provider_user_id=str(payload.get("id", email)),
        email=email,
        name=payload.get("name", email.split("@")[0]),
        avatar_url=payload.get("picture", ""),
    )
    login_user(user_id)

    return redirect(url_for("index"))


@app.route("/auth/github")
def auth_github():
    if not OAUTH_PROVIDERS["github"]:
        return render_template("login.html", error="GitHub OAuth is not configured.", google_enabled=OAUTH_PROVIDERS["google"], github_enabled=False, oauth_hint="Set GITHUB_OAUTH_CLIENT_ID and GITHUB_OAUTH_CLIENT_SECRET.")

    if not github.authorized:
        oauth_callback_uri = url_for(
            "github.authorized",
            _external=True,
            _scheme=app.config.get("PREFERRED_URL_SCHEME", "https"),
        )
        provider_login_url = url_for(
            "github.login",
            _external=True,
            _scheme=app.config.get("PREFERRED_URL_SCHEME", "https"),
        )
        app.logger.info(
            "OAuth start | provider=github fixed_redirect_uri=%s generated_oauth_callback_uri=%s provider_login_url=%s request_host=%s",
            get_provider_redirect_uri("github"),
            oauth_callback_uri,
            provider_login_url,
            request.host,
        )
        return redirect(provider_login_url)

    user_resp = github.get("/user")
    if not user_resp.ok:
        return render_template("login.html", error="GitHub authentication failed.", google_enabled=OAUTH_PROVIDERS["google"], github_enabled=OAUTH_PROVIDERS["github"], oauth_hint="Try again or verify OAuth app callback URL.")

    user_data = user_resp.json()
    email = user_data.get("email")

    if not email:
        emails_resp = github.get("/user/emails")
        if emails_resp.ok:
            emails = emails_resp.json()
            primary = next((item for item in emails if item.get("primary")), None)
            email = (primary or (emails[0] if emails else {})).get("email")

    if not email:
        return render_template("login.html", error="GitHub email not available.", google_enabled=OAUTH_PROVIDERS["google"], github_enabled=OAUTH_PROVIDERS["github"], oauth_hint="Enable email scope and ensure email is visible on GitHub.")

    user_id = upsert_user(
        provider="github",
        provider_user_id=str(user_data.get("id", email)),
        email=email,
        name=user_data.get("name") or user_data.get("login") or email.split("@")[0],
        avatar_url=user_data.get("avatar_url", ""),
    )
    login_user(user_id)

    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    for key in [
        "user_id",
        "user_name",
        "user_email",
        "user_avatar",
        "uploaded_path",
        "uploaded_filename",
        "google_oauth_token",
        "github_oauth_token",
    ]:
        session.pop(key, None)
    return redirect(url_for("login"))


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    if "dataset" not in request.files:
        return render_template("index.html", error="No file part found in request.", user=g.user)

    file = request.files["dataset"]

    if file.filename == "":
        return render_template("index.html", error="Please choose a CSV file to upload.", user=g.user)

    if not allowed_file(file.filename):
        return render_template("index.html", error="Only CSV files are supported.", user=g.user)

    saved_path = os.path.join(UPLOAD_DIR, "uploaded_dataset.csv")
    file.save(saved_path)
    session["uploaded_path"] = saved_path
    session["uploaded_filename"] = file.filename

    db = get_db()
    db.execute(
        """
        INSERT INTO uploaded_datasets (user_id, original_filename, stored_path, uploaded_at)
        VALUES (?, ?, ?, ?)
        """,
        (g.user["id"], file.filename, saved_path, current_timestamp()),
    )
    db.commit()

    return redirect(url_for("process"))


@app.route("/process")
@login_required
def process():
    uploaded_path = session.get("uploaded_path")
    if not uploaded_path or not os.path.exists(uploaded_path):
        return render_template("index.html", error="Upload a valid dataset first.", user=g.user)

    try:
        result = build_dashboard_payload(uploaded_path)
        return render_template(
            "index.html",
            success=True,
            initial_data=result,
            uploaded_filename=session.get("uploaded_filename", "uploaded_dataset.csv"),
            user=g.user,
        )
    except Exception as exc:
        return render_template("index.html", error=f"Processing failed: {str(exc)}", user=g.user)


@app.route("/api/process")
@api_login_required
def api_process():
    uploaded_path = session.get("uploaded_path")
    if not uploaded_path or not os.path.exists(uploaded_path):
        return jsonify({"error": "Upload a valid dataset first."}), 400

    try:
        selected_k = request.args.get("k", type=int)
        result = build_dashboard_payload(uploaded_path, selected_k=selected_k)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/download/csv")
@login_required
def download_csv():
    uploaded_path = session.get("uploaded_path")
    if not uploaded_path or not os.path.exists(uploaded_path):
        return render_template("index.html", error="Upload and process a dataset before downloading CSV.")

    try:
        selected_k = request.args.get("k", type=int)
        result = build_dashboard_payload(uploaded_path, selected_k=selected_k)
        rows_df = pd.DataFrame(result["table_rows"])

        csv_buffer = StringIO()
        rows_df.to_csv(csv_buffer, index=False)
        csv_bytes = csv_buffer.getvalue().encode("utf-8")

        return send_file(
            io.BytesIO(csv_bytes),
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"clustered_customers_k{result['selected_k']}.csv",
        )
    except Exception as exc:
        return render_template("index.html", error=f"CSV download failed: {str(exc)}", user=g.user)


def build_pdf_report(result: dict) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    margin_x = 28
    margin_top = 32
    margin_bottom = 28
    y = height - margin_top

    def decorate_current_page():
        page_number = pdf.getPageNumber()
        border_margin = 16

        pdf.setStrokeColor(colors.HexColor("#8FA3C9"))
        pdf.setLineWidth(1)
        pdf.rect(
            border_margin,
            border_margin,
            width - (2 * border_margin),
            height - (2 * border_margin),
            fill=0,
            stroke=1,
        )

        pdf.setFont("Helvetica", 9)
        pdf.setFillColor(colors.HexColor("#506080"))
        pdf.drawRightString(width - border_margin - 4, border_margin + 3, f"Page {page_number}")

    decorate_current_page()

    def new_page_if_needed(required_space=24):
        nonlocal y
        if y < margin_bottom + required_space:
            pdf.showPage()
            y = height - margin_top
            decorate_current_page()

    def add_line(text: str, size: int = 10, bold: bool = False, gap: int = 14):
        nonlocal y
        new_page_if_needed(gap)
        font_name = "Helvetica-Bold" if bold else "Helvetica"
        pdf.setFont(font_name, size)
        pdf.drawString(margin_x, y, text)
        y -= gap

    def draw_table_header(col_x, row_h=18):
        nonlocal y
        headers = ["S.No", "Income (k$)", "Spending Score", "Cluster", "Segment"]
        new_page_if_needed(row_h + 4)
        y_top = y
        y_bottom = y - row_h

        pdf.setFillColor(colors.HexColor("#E9EEF7"))
        pdf.rect(margin_x, y_bottom, width - (2 * margin_x), row_h, fill=1, stroke=0)

        pdf.setStrokeColor(colors.HexColor("#B7C3D8"))
        pdf.setLineWidth(0.8)
        pdf.rect(margin_x, y_bottom, width - (2 * margin_x), row_h, fill=0, stroke=1)

        for x in col_x[1:-1]:
            pdf.line(x, y_bottom, x, y_top)

        pdf.setFont("Helvetica-Bold", 9)
        pdf.setFillColor(colors.black)
        for i, head in enumerate(headers):
            pdf.drawString(col_x[i] + 4, y_bottom + 5, head)

        y -= row_h

    def draw_table_row(values, col_x, row_h=16):
        nonlocal y
        new_page_if_needed(row_h + 2)
        y_top = y
        y_bottom = y - row_h

        pdf.setStrokeColor(colors.HexColor("#D5DCE9"))
        pdf.setLineWidth(0.6)
        pdf.rect(margin_x, y_bottom, width - (2 * margin_x), row_h, fill=0, stroke=1)
        for x in col_x[1:-1]:
            pdf.line(x, y_bottom, x, y_top)

        pdf.setFont("Helvetica", 8.8)
        pdf.setFillColor(colors.black)
        for i, value in enumerate(values):
            text = str(value)
            if len(text) > 28:
                text = text[:25] + "..."
            pdf.drawString(col_x[i] + 4, y_bottom + 4.5, text)

        y -= row_h

    add_line("Customer Segmentation Report", size=16, bold=True, gap=20)
    add_line(f"Generated at: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    add_line(f"Selected K: {result['selected_k']}")
    add_line(f"Optimal K: {result['optimal_k']}")
    add_line("")

    analytics = result["analytics"]
    add_line("Analytics Summary", size=12, bold=True, gap=18)
    add_line(f"Total Customers: {analytics['total_customers']}")
    add_line(f"Average Income: {analytics['average_income']}")
    add_line(f"Average Spending Score: {analytics['average_spending_score']}")
    add_line("")

    add_line("Cluster Insights", size=12, bold=True, gap=18)
    for cluster in result["cluster_details"]:
        add_line(
            f"Cluster {cluster['cluster']} | {cluster['segment']} | {cluster['percentage']}% | Avg Income {cluster['avg_income']} | Avg Score {cluster['avg_score']}",
            size=9,
            bold=True,
            gap=12,
        )
        for line in wrap(f"Recommendation: {cluster['recommendation']}", 100):
            add_line(line, size=9, gap=11)
        add_line("", gap=8)

    add_line("Clustered Customer Table", size=12, bold=True, gap=18)

    table_width = width - (2 * margin_x)
    col_widths = [0.08, 0.2, 0.24, 0.16, 0.32]
    col_x = [margin_x]
    running_x = margin_x
    for ratio in col_widths:
        running_x += table_width * ratio
        col_x.append(running_x)

    cluster_to_segment = {item["cluster"]: item["segment"] for item in result["cluster_details"]}

    draw_table_header(col_x)
    for idx, row in enumerate(result["table_rows"], start=1):
        if y < margin_bottom + 20:
            pdf.showPage()
            y = height - margin_top
            decorate_current_page()
            draw_table_header(col_x)

        draw_table_row(
            [
                idx,
                row["Annual Income (k$)"],
                row["Spending Score (1-100)"],
                row["Cluster"],
                cluster_to_segment.get(row["Cluster"], "-")
            ],
            col_x,
        )

    pdf.save()
    buffer.seek(0)
    return buffer.getvalue()


@app.route("/download/pdf")
@login_required
def download_pdf():
    uploaded_path = session.get("uploaded_path")
    if not uploaded_path or not os.path.exists(uploaded_path):
        return render_template("index.html", error="Upload and process a dataset before downloading PDF.", user=g.user)

    try:
        selected_k = request.args.get("k", type=int)
        result = build_dashboard_payload(uploaded_path, selected_k=selected_k)
        pdf_bytes = build_pdf_report(result)
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"customer_segmentation_report_k{result['selected_k']}.pdf",
        )
    except Exception as exc:
        return render_template("index.html", error=f"PDF download failed: {str(exc)}", user=g.user)


@app.route("/api/save-result", methods=["POST"])
@api_login_required
def save_result():
    uploaded_path = session.get("uploaded_path")
    if not uploaded_path or not os.path.exists(uploaded_path):
        return jsonify({"error": "Upload and process a dataset first."}), 400

    payload = request.get_json(silent=True) or {}
    selected_k = payload.get("selected_k")

    try:
        result = build_dashboard_payload(uploaded_path, selected_k=selected_k)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    db = get_db()
    db.execute(
        """
        INSERT INTO results (user_id, selected_k, optimal_k, total_customers, average_income, average_spending_score, cluster_details, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            g.user["id"],
            result["selected_k"],
            result["optimal_k"],
            result["analytics"]["total_customers"],
            result["analytics"]["average_income"],
            result["analytics"]["average_spending_score"],
            json.dumps(result["cluster_details"]),
            current_timestamp(),
        ),
    )
    db.commit()

    return jsonify({"message": "Result saved.", "selected_k": result["selected_k"]})


@app.route("/api/my-results")
@api_login_required
def my_results():
    db = get_db()
    rows = db.execute(
        """
        SELECT id, selected_k, optimal_k, total_customers, average_income, average_spending_score, created_at
        FROM results
        WHERE user_id = ?
        ORDER BY datetime(created_at) DESC
        LIMIT 20
        """,
        (g.user["id"],),
    ).fetchall()

    return jsonify(
        [
            {
                "id": row["id"],
                "selected_k": row["selected_k"],
                "optimal_k": row["optimal_k"],
                "total_customers": row["total_customers"],
                "average_income": row["average_income"],
                "average_spending_score": row["average_spending_score"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
