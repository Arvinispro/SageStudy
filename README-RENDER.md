Deployment on Render
--------------------

Quick steps:

- Ensure this repo is pushed to GitHub.
- In Render dashboard, create a new Web Service and connect your GitHub repo.
- Render will detect `render.yaml` and use that configuration. If you prefer manual settings, set:
  - Environment: `Python 3`
  - Build command: `pip install -r requirements.txt`
  - Start command: `gunicorn -w 4 -k uvicorn.workers.UvicornWorker main:app`
- Add any required environment variables in the Render service settings (Supabase keys, OpenAI keys, etc.).

Local test:

```bash
# create venv, install deps
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# run with the same command used on Render
gunicorn -w 4 -k uvicorn.workers.UvicornWorker main:app
```
