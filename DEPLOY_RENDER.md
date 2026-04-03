# Render Deployment Guide (Flask + OAuth)

## 1) Push project to GitHub

Use a repository that contains this project folder as its root.

## 2) Create Render Web Service

- In Render, click New + -> Web Service
- Connect your GitHub repository
- Select the branch to deploy

## 3) Build and start configuration

- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app --bind 0.0.0.0:$PORT`

If using Blueprint deploy, Render can read these from `render.yaml`.

## 4) Environment variables in Render

Set these in Render service settings:

- SECRET_KEY
- APP_BASE_URL=https://your-service-name.onrender.com
- GOOGLE_OAUTH_CLIENT_ID
- GOOGLE_OAUTH_CLIENT_SECRET
- GITHUB_OAUTH_CLIENT_ID
- GITHUB_OAUTH_CLIENT_SECRET
- SESSION_COOKIE_SECURE=true
- FLASK_ENV=production
- DATA_DIR=/var/data/ml-project

## 5) OAuth provider callback URLs

Google Cloud Console:

- Authorized redirect URI must be exactly:
  https://your-service-name.onrender.com/login/google/authorized

GitHub OAuth App:

- Authorization callback URL must be exactly:
  https://your-service-name.onrender.com/login/github/authorized

## 6) Deploy and verify

- Trigger deploy in Render
- Open only the Render domain (not preview or localhost)
- Test both Sign in with Google and Sign in with GitHub

## 7) Common checks if login fails

- APP_BASE_URL exactly matches your Render domain
- Redirect URI path is exactly `/login/google/authorized` and `/login/github/authorized`
- Google and GitHub app settings use HTTPS, same domain, same path
- SECRET_KEY is set and non-empty
