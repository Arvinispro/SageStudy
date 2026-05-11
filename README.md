# SageStudy

SageStudy is a full-stack study companion with Supabase authentication, learner profiles, practice sessions, and study tools. The app is deployed and available here: https://sagestudy.onrender.com/frontend/menu/menu.html

## Highlights

- Supabase-backed sign up, sign in, session lookup, and log out flows
- Profile and learning preference management
- Practice and study mode UI pages for focused learning workflows
- FastAPI backend with modular services and routers
- Automated tests covering the auth flow and common failure cases

## Live App

Open the deployed experience here:

- https://sagestudy.onrender.com

## Tech Stack

- Backend: FastAPI, Python 3.11+
- Authentication: Supabase Auth and JWT verification
- Data: Supabase Postgres
- Validation: Pydantic v2
- Testing: pytest
- Deployment: Render

## Project Structure

- `main.py` - application entrypoint
- `app/config.py` - environment configuration
- `app/models.py` - shared request and response schemas
- `app/routers/` - HTTP route handlers
- `app/services/` - auth, course, orchestration, and storage logic
- `frontend/` - static UI pages for the study experience
- `tests/` - auth test coverage
- `tables.sql` - database schema source

## Features

- User authentication with Supabase
- Profile persistence and account page support
- Learning preference capture
- Study and practice entry points for learners
- Backend API designed for Render deployment

## Local Setup

1. Create and activate a Python virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create your environment file:

```bash
cp .env.example .env
```

4. Add your Supabase credentials:

```env
SUPABASE_URL=https://YOUR_PROJECT_REF.supabase.co
SUPABASE_DEFAULT_KEY=YOUR_SUPABASE_DEFAULT_KEY
SUPABASE_SERVICE_ROLE_KEY=YOUR_SUPABASE_SERVICE_ROLE_KEY
JWT_SECRET=YOUR_SUPABASE_JWT_SECRET
```

## Run Locally

Start the API server:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open the API docs at:

- http://127.0.0.1:8000/docs

## Database Migration

Apply the schema in Supabase SQL Editor using `tables.sql`, or adapt it into your preferred migration workflow.

## API Overview

Base path: `/api/v1/auth`

- `POST /sign_up` - create a new account
- `POST /sign_in` - authenticate a user
- `GET /current_user` - fetch the active user from the bearer token
- `POST /log_out` - invalidate the current session

Example sign up request:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/sign_up \
  -H "Content-Type: application/json" \
  -d '{
    "email": "alice@example.com",
    "password": "Password123",
    "username": "alice_01"
  }'
```

## Testing

Run the test suite with:

```bash
pytest -q
```

## Notes

- The current schema is taken directly from `tables.sql`.
- The codebase is structured to support auth, profile, study, and practice features as the app grows.