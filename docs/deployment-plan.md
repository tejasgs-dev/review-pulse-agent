# Deployment Plan (free plans only)

*Status: draft for approval, 2026-11-28. Nothing here is built or deployed yet.*

This plan puts ReviewPulse online using only free plans. It is written for someone deploying for the first time. Every platform setting in it was checked against the current Vercel, Render and GitHub docs on 2026-11-28 (links in each section).

**What stays the same.** The weekly job that does the real work (reading reviews, machine learning, writing with the LLM, publishing to Google Docs and Gmail) runs on **GitHub Actions** only. The website keeps reading the JSON files in `site/pulses/` that `pulse export-site` writes. The MCP server (`gworkspace-mcp`) on Render is not changed.

**What is new.**

- The website (`site/`) moves to **Vercel**.
- A small **backend API** on **Render** adds live information the static files can't hold, such as "did last week's run succeed?"
**Already done before deployment.** The weekly workflow **`.github/workflows/pulse-check.yml`** was built first, as Phase 4 of the agent upgrade (2026-11-28). There is **only one** `pulse-check.yml`. Deployment reuses it as it is and adds no second workflow; Step 2 only adds the backend and the frontend changes. Its four secrets are set up with it (§4.1).

---

## 1. The four parts and how they talk

```
                          (1) opens the page
   ┌──────────┐  ─────────────────────────────▶ ┌──────────────────────────────┐
   │ Visitor's│                                  │ VERCEL  (frontend, free)     │
   │ browser  │ ◀───── HTML, CSS, JS, and ────── │ site/  + site/pulses/*.json  │
   │          │        the pulse JSON (instant)  └──────────────────────────────┘
   │          │                                                ▲
   │          │  (2) one request after the page is drawn       │ (5) redeploys on every
   │          │  ─────────────────────────────▶ ┌──────────────┴───────────────┐  push to main
   │          │ ◀──── run status, MCP health ── │ RENDER  backend API (free)   │
   └──────────┘   (may take ~1 min to wake up)  │ "review-pulse-api"           │
                                                │ read-only; can't run the job │
                                                └───┬──────────────────┬───────┘
                       (3) asks "how did the last   │                  │ (4) GET /healthz
                           pulse-check run go?"     ▼                  ▼     only when asked
                                        ┌────────────────────┐  ┌────────────────────────┐
                                        │ GITHUB             │  │ RENDER  MCP server     │
                                        │ Actions REST API   │  │ gworkspace-mcp         │
                                        └────────────────────┘  │ (unchanged)            │
                                                                 └────────────────────────┘
                                                                           ▲
   ┌───────────────────────────────────────────────┐   (6) publishes the   │
   │ GITHUB ACTIONS  pulse-check.yml               │ ──── Doc entry and ───┘
   │ every Monday 09:00 IST (and by hand)          │      the email
   │ pulse doctor → pulse run → pulse export-site  │
   │ → commits site/pulses/ to main ───────────────┼──▶ (5) Vercel and Render redeploy
   └───────────────────────────────────────────────┘        with the new week
```

Step by step:

1. A visitor opens the Vercel site. The page and the pulse data come straight from Vercel, so the dashboard appears at once, even if Render is asleep or down.
2. After the page is drawn, the browser sends **one** request to the Render backend (`/api/status`). If Render is asleep, this request wakes it. The page shows *"Waking up the server (free plan, ~1 minute)…"* until the answer arrives, then adds the extra information.
3. The backend asks GitHub's API how the latest `pulse-check.yml` run went.
4. Only when the visitor opens the **System status** panel, the backend checks the MCP server's `/healthz`. (Why only then? See §5.)
5. Each Monday the workflow commits the new week's JSON to `main`. Vercel and Render notice the push and redeploy, so both serve the new week.
6. The same workflow publishes to Google Docs and Gmail through the MCP server, as it does today.

**Who holds which keys.** Only GitHub Actions holds the keys that can publish or spend money (Groq, MCP key, Doc ID, recipient). The backend has no MCP key and no LLM key, so even if someone tried, it **can't** publish, email or run the pulse.

---

## 2. What the backend does, and where its data comes from

### The endpoints (all read-only, `GET` only)

| Endpoint | What it answers | Where the answer comes from |
|---|---|---|
| `/healthz` | `{"status": "ok"}` if the backend is up. Used by Render's health check. | Nothing (no outside calls, so it's fast) |
| `/api/pulses` | The list of published weeks (same as `index.json`) | `site/pulses/index.json` in the backend's copy of the repo |
| `/api/pulses/latest` | The newest week's full pulse | `site/pulses/{latest}.json` |
| `/api/pulses/{week}` | One week, e.g. `2026-W46`. Bad week names → 400, unknown week → 404 | `site/pulses/{week}.json` |
| `/api/status` | The latest `pulse-check.yml` run (result, when, link to it), the latest published week, the backend's version | GitHub REST API, `GET /repos/tejasgs-dev/review-pulse-agent/actions/workflows/pulse-check.yml/runs?per_page=1` |
| `/api/mcp-health` | Is the MCP server online, still waking, or offline? | `GET https://gworkspace-mcp.onrender.com/healthz`, 15-second limit |

Answers from GitHub and the MCP server are **remembered for 5 minutes** (a small in-memory cache), so many visitors don't mean many outside calls. The cache is lost when the service sleeps, which is fine.

The site itself uses `/api/status` and `/api/mcp-health`. The `/api/pulses` endpoints are a small public API for anyone (or any tool) that wants the data as JSON. The site keeps using its own static files for the pulse, so it never depends on the backend for that.

### What the site shows with it

- **Last weekly run**: "✓ Succeeded · Mon 23 Nov, 09:04 IST · View run" (or "✗ Failed", "Running now…", "Not set up yet" if the workflow has never run).
- **MCP server**: Online / Asleep or waking / Offline. Checked only when the System status panel is opened.
- **A banner if the site's data is out of date**: if GitHub says the last run succeeded after the week the page shows was published, the page suggests pressing Refresh.

### Why this is the simplest free option

- **No database, no storage.** The backend reads the same JSON files the site uses, from its own copy of the repo. Render's free disk is wiped on every restart anyway, and we never need to write anything.
- **New data arrives by itself.** Render redeploys when the workflow commits new files, so there's no sync job and no extra secret.
- **Small and cheap to wake.** Python + FastAPI (the framework Render's own guide uses) + `httpx`. It has its own short `backend/requirements.txt`, so Render does *not* install the heavy ML packages (`scikit-learn`, `model2vec`, `pandas`…). The build takes about a minute and the service starts fast.
- **It never runs the job.** It has no LLM or MCP keys and no endpoint that writes anything.

**Where the code goes**: a new `backend/` folder (`backend/app.py`, `backend/requirements.txt`), plus tests in `tests/test_backend.py` that run in the existing CI. FastAPI is added to the `dev` extras so CI can test it.

---

## 3. Every environment variable and secret

No real values here, ever. Values go only into the platform dashboards.

### GitHub → repository secrets (for `pulse-check.yml`)

| Name | Required? | What it's for |
|---|---|---|
| `GROQ_API_KEY` | Yes | The free Groq LLM that writes the pulse and drives the publishing agent. Without it the run falls back to rules and the scripted publisher. |
| `PULSE_MCP_API_KEY` | Yes | The key the MCP server checks (it equals the server's `MCP_API_KEY`). |
| `PULSE_DOC_ID` | Yes | The Google Doc the pulse is appended to. |
| `PULSE_RECIPIENT` | Yes | The one email address the pulse is sent to. |
| `ANTHROPIC_API_KEY` | No | Only for `--engine llm` with the Claude provider. Leave it out to stay free. |
| `GITHUB_TOKEN` | Automatic | GitHub creates it for each run; don't add it. The workflow uses it to commit `site/pulses/` (`permissions: contents: write`). |

### Render → backend service (`review-pulse-api`) → Environment

| Name | Required? | What it's for |
|---|---|---|
| `ALLOWED_ORIGINS` | Yes | The only website allowed to call the API from a browser (CORS). Your exact Vercel address, e.g. `https://review-pulse.vercel.app`: no path, no slash at the end. For more than one, separate with commas. |
| `PYTHON_VERSION` | Yes | Pins Python, e.g. `3.12.8`. Render needs the full `x.y.z` form. Without it Render uses its newest default (3.14.10 for new services). |
| `GITHUB_REPO` | No (default `tejasgs-dev/review-pulse-agent`) | The repo whose workflow runs are shown. |
| `GITHUB_WORKFLOW` | No (default `pulse-check.yml`) | The workflow file to report on. |
| `GITHUB_TOKEN_READONLY` | No, recommended | A fine-grained GitHub token with **read-only** access to this repo's Actions. Without it the backend uses GitHub's anonymous limit (60 requests an hour, shared with other Render users on the same outgoing IP). With it, 5,000 an hour. *Not* named `GITHUB_TOKEN`, to avoid confusion with the Actions token. |
| `MCP_HEALTH_URL` | No (default `https://gworkspace-mcp.onrender.com/healthz`) | Where to check the MCP server. |

### Vercel → frontend project

| Name | What it's for |
|---|---|
| *(none)* | The site has no build step and no secrets. The backend's address is public, so it's written in `site/assets/app.js` (and in the page's Content-Security-Policy). It isn't a secret: anyone can see it in the browser anyway. |

### The MCP server on Render

No changes. It keeps its own variables (`MCP_API_KEY`, `ALLOWED_RECIPIENTS`, `ENABLE_SEND_EMAIL`, Google credentials).

---

## 4. Click-by-click setup

The GitHub secrets (§4.1) and the first manual run (§4.5) come first, with `pulse-check.yml`, before Step 2. The rest happens **after** Step 2 is built and committed, in this order: Render backend → Vercel → update CORS on Render.

### 4.1 GitHub: add the secrets

*(Docs: [Using secrets in GitHub Actions](https://docs.github.com/en/actions/security-for-github-actions/security-guides/using-secrets-in-github-actions).)*

1. Open `https://github.com/tejasgs-dev/review-pulse-agent`.
2. Click **Settings** (top bar; if you can't see it, it's under the **⋯** menu).
3. In the left sidebar, under **Security**, click **Secrets and variables** → **Actions**.
4. On the **Secrets** tab, click **New repository secret**.
5. **Name**: `GROQ_API_KEY`. **Secret**: paste the key. Click **Add secret**.
6. Repeat for `PULSE_MCP_API_KEY`, `PULSE_DOC_ID` and `PULSE_RECIPIENT`.

Names may hold only letters, digits and `_`, and can't start with `GITHUB_`. GitHub never shows a secret again after you save it. To change one, click it and choose **Update**.

**Optional, read-only token for the backend** (for `GITHUB_TOKEN_READONLY`). This token is **not** a GitHub Actions secret. It goes only on Render (§4.2), because only the backend uses it.

1. On GitHub, click your avatar (top right) → **Settings**.
2. Left sidebar, at the bottom: **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
3. **Token name**: `review-pulse-api read-only`. **Description**: "Render backend reads pulse-check.yml run status".
4. **Resource owner**: your account (`tejasgs-dev`).
5. **Expiration**: pick a date (up to 1 year), and put a reminder in your calendar for that day.
6. **Repository access**: **Only select repositories** → choose `tejasgs-dev/review-pulse-agent` only.
7. **Permissions** → **Repository permissions** → find **Actions** → set it to **Read-only**. GitHub adds **Metadata: Read-only** by itself; that's required and harmless. Leave everything else as **No access** (no Contents, no Secrets, no Workflows).
8. Click **Generate token**. Copy it right away: GitHub shows it only once.
9. Paste it on Render as `GITHUB_TOKEN_READONLY` (§4.2, step 5). Don't put it in `.env`, in GitHub secrets, or in any file in the repo.

If it leaks, it can only read this public repo's Actions runs, which anyone can see anyway. Still, delete it at once from the same page, then make a new one.

### 4.2 Render: create the backend

*(Docs: [Web services](https://render.com/docs/web-services), [Deploy FastAPI](https://render.com/docs/deploy-fastapi), [Monorepo support](https://render.com/docs/monorepo-support), [Free instances](https://render.com/docs/free).)*

1. Sign in at `https://dashboard.render.com` (the same workspace as `gworkspace-mcp` is fine; see §5 on shared hours).
2. Click **New** → **Web Service**.
3. On the **Git Provider** tab, pick `tejasgs-dev/review-pulse-agent` (click **Configure GitHub** first if Render can't see it yet).
4. Fill in the form:
   - **Name**: `review-pulse-api`. Your address becomes `https://review-pulse-api.onrender.com` (if the name is taken, Render adds a suffix; use the address it shows).
   - **Region**: the same region as `gworkspace-mcp` (e.g. *Singapore*).
   - **Branch**: `main`
   - **Root Directory**: `backend` (if the field isn't on this form, set it after creation in **Settings → Build → Root Directory**).
   - **Language**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn app:app --host 0.0.0.0 --port $PORT`
   - **Instance type / compute plan**: **Free**
5. Open **Advanced**:
   - **Environment Variables** → **Add Environment Variable** for each row of the Render table in §3. For `ALLOWED_ORIGINS`, put a placeholder `https://example.invalid` for now; you'll fix it in step 4.4.
   - **Health Check Path**: `/healthz` (if offered).
6. Click **Deploy web service** and wait for the log to say *"Your service is live"*.
7. **Settings → Build → Build Filters → Edit**:
   - **+ Add Included Path** `backend/**`
   - **+ Add Included Path** `site/pulses/**`
   - **Save Changes**.

   This makes Render redeploy only when the backend code or the pulse data changes, not on every edit to docs or the ML code. Build filter paths are relative to the repo root, even with a Root Directory set. With Root Directory `backend`, a push changing only `site/pulses/` would otherwise **not** redeploy.
8. **Settings → Auto-Deploy**: leave **On Commit** (the default).
9. Open `https://review-pulse-api.onrender.com/healthz` in your browser. You should see `{"status":"ok"}`.

### 4.3 Vercel: create the frontend

*(Docs: [Importing a project](https://vercel.com/docs/getting-started-with-vercel/import), [Configuring a build](https://vercel.com/docs/builds/configure-a-build), [Generated URLs](https://vercel.com/docs/deployments/generated-urls).)*

1. Sign up or sign in at `https://vercel.com` with **Continue with GitHub** (Hobby plan, free).
2. Go to `https://vercel.com/new` (or **Add New…** → **Project**).
3. Find `review-pulse-agent` and click **Import** (grant Vercel access to the repo if asked).
4. On the **Configure Project** screen:
   - **Project Name**: `review-pulse` (this becomes your address).
   - **Framework Preset**: **Other**.
   - **Root Directory**: click **Edit** → choose `site` → **Continue**.
   - **Build and Output Settings**: turn on **Override** for **Build Command** and leave it **empty**. Leave **Output Directory** as it is (`.` means the `site` folder itself).
   - **Environment Variables**: none.
5. Click **Deploy**. After about 30 seconds you'll see a preview of the site.
6. Go to the project → **Settings** → **Domains** and copy the production address exactly (e.g. `https://review-pulse.vercel.app`). Vercel may add your account name to it (`review-pulse-tejasgs-dev.vercel.app`), so copy what it shows rather than guessing.

From now on, every push to `main` redeploys the site within a minute. Other branches get *preview* addresses, which the backend's CORS rule will block on purpose (the static pulse still works on them).

### 4.4 Render: allow your Vercel address (CORS)

1. Render → `review-pulse-api` → **Environment** → **Edit**.
2. Set `ALLOWED_ORIGINS` to the exact Vercel address from 4.3 step 6, starting with `https://`, with **no** `/` at the end.
3. Click **Save, rebuild, and deploy**.

### 4.5 The first weekly run

1. GitHub → **Actions** → **Pulse check** (left list) → **Run workflow** → branch `main` → **Run workflow**.
2. Wait until it's green (about 3–5 minutes). The job page shows the run summary.
3. It commits `site/pulses/` only if **this run appended** the week to the Doc. If the week is already in the Doc, it skips, and that's still a success.
4. After that it runs by itself every Monday at 09:00 IST (03:30 UTC). GitHub can start scheduled runs late when it's busy, sometimes by many minutes.

### 4.6 The `pulse-check.yml` workflow (already built; Step 2 reuses it)

Built in agent upgrade Phase 4, before deployment. Step 2 doesn't create or change it; the backend only *reads* its run status from GitHub's API.

In short: it runs on `schedule` (`30 3 * * 1`) and on `workflow_dispatch` (the **Run workflow** button, with optional `week` and `dry_run`). It installs the project, restores the cached embedding model, and runs `pulse doctor`, `pulse run --engine hybrid --mode agent` and `pulse export-site --week <that week>`. Then it commits `site/pulses/` as `github-actions[bot]`. It has `permissions: contents: write` and `concurrency`, so two runs never overlap. It never uploads `runs/` or `data/`, because those hold review text.

---

## 5. Free-tier limits that matter

| Limit (checked 2026-11-28) | Why it matters | How this plan stays within it |
|---|---|---|
| **Render: a free web service sleeps after 15 minutes with no requests and takes about 1 minute to wake.** | The first visitor after a quiet spell waits for the backend. | The page never waits for it: the pulse comes from Vercel at once. Only the extra info waits, behind the "Waking up the server…" message. |
| **Render: 750 free instance hours per month per workspace, shared by all free services.** A sleeping service uses none. If they run out, Render suspends all free services until the 1st of next month. | A month is 720–744 hours. One service that never sleeps would use almost all of it and starve the MCP server, and then the weekly publish would fail. | **No polling, no auto-refresh, no uptime pingers** (don't add UptimeRobot or cron-job.org "keep-alive" pings). Each visit keeps the backend awake for at most ~15 minutes. Even 20 separate visits a day is about 150 hours a month. The MCP server is checked only when someone opens the status panel, not on every page load, so visits don't keep it awake too. The weekly run wakes MCP for about 20 minutes. Total: far under 750. |
| **Render free: 0.1 CPU, 512 MB RAM, may restart at any time, disk is wiped on restart.** | Heavy work would be slow or crash. | The backend only reads small JSON files and makes two small HTTP calls. Its cache lives in memory and is fine to lose. |
| **Render: build minutes and outbound bandwidth have a monthly free amount** (shown under *Billing → Monthly Included Usage*). | Running out stops new builds. | Build filters mean about one deploy a week, each about a minute. Answers are a few kilobytes. |
| **Vercel Hobby: 100 GB data transfer per month, 100 deployments per day.** | Big traffic or many pushes. | The site is under 1 MB. We deploy about once a week plus your own pushes. |
| **Vercel Hobby can't connect repos owned by a GitHub organization.** | Import would fail. | `tejasgs-dev` is a personal account (checked), so it's fine. |
| **GitHub Actions: free for public repos. A scheduled workflow is switched off after 60 days with no repo activity. Scheduled runs can start late.** | The weekly run could stop silently. | The weekly commit to `site/pulses/` counts as activity. The site shows the last run's date, so a missed run is visible. |
| **GitHub API: 60 requests per hour per IP without a token; 5,000 with a token.** | Render's outgoing IPs are shared, so the anonymous limit can run out. | 5-minute cache, plus the optional read-only token. If GitHub refuses, the page says "Run status unavailable" and everything else still works. |

---

## 6. How to check it works

Tick each one after setup.

**Backend (Render)**
- [ ] `https://<backend>/healthz` shows `{"status":"ok"}`.
- [ ] `https://<backend>/api/pulses` lists `2026-W46` (and any newer weeks).
- [ ] `https://<backend>/api/pulses/latest` shows the newest week.
- [ ] `https://<backend>/api/pulses/nope` returns an error (400), not a crash.
- [ ] `https://<backend>/api/status` shows the last `pulse-check.yml` run (or "not set up yet" before the first run).
- [ ] `https://<backend>/api/mcp-health` shows `online` (it may take up to a minute if MCP is asleep).
- [ ] The Render **Logs** tab shows no errors and no secret values.

**Frontend (Vercel)**
- [ ] The Vercel address opens the Dashboard with the latest pulse **right away**, before the backend answers.
- [ ] After 15+ quiet minutes, reload: *"Waking up the server (free plan, ~1 minute)…"* appears, then the run status replaces it.
- [ ] **Refresh** reloads the data once. Nothing reloads by itself: in DevTools → Network, no new requests appear while you wait 5 minutes.
- [ ] Opening **System status** checks the MCP server once and shows Online.
- [ ] **Past weeks** and **How it works** pages open. Links to the workflow and the Doc work.
- [ ] DevTools → Console shows no CORS or Content-Security-Policy errors.

**Down-backend test**
- [ ] On Render, click **Suspend** on `review-pulse-api`, reload the site: the pulse still shows, with a calm "Live status unavailable" note. Click **Resume** afterwards.

**CORS test**
- [ ] On a Vercel *preview* address (or `http://localhost:8000`), the console shows the backend request blocked by CORS, and the pulse still shows. This proves only your production address is allowed.

**Weekly job (GitHub Actions)**
- [ ] A manual **Run workflow** goes green.
- [ ] If it published a new week: a commit "Add the 20xx-Wxx pulse to the site" appears on `main`. Vercel and Render both redeploy, and the new week shows on the site and at `/api/pulses/latest`.
- [ ] The Doc entry and the email arrive (or "already published" is logged).
- [ ] The Render backend logs show **no** pulse commands. It only answers GET requests.

**Secrets**
- [ ] `git grep -i -E "gsk_|api_key=|@gmail" -- . ':!*.example'` finds nothing new. `.env` is still git-ignored.

---

## 7. Common errors and fixes

| What you see | Likely cause | Fix |
|---|---|---|
| Console: *"blocked by CORS policy: No 'Access-Control-Allow-Origin' header"* | `ALLOWED_ORIGINS` doesn't exactly match the address in the browser bar. | Copy the address exactly: `https://`, no `/` at the end, no `www`. Save and redeploy on Render. Preview addresses are blocked on purpose. |
| Console: *"Refused to connect … violates Content Security Policy"* | The backend address in the page's CSP `connect-src` doesn't match the real Render address. | Update the address in `app.js` and in the CSP `<meta>` of the three HTML pages, then push. |
| "Waking up the server…" never ends, then "Live status unavailable" | The backend failed to start, or it's suspended (hours ran out, or you suspended it). | Render → `review-pulse-api` → **Logs** and **Events**. Check the free hours on the Billing page. Static data still shows meanwhile. |
| Render build fails: *"No matching distribution"* or *"ModuleNotFoundError: fastapi"* | Wrong Root Directory, so the wrong `requirements.txt` was installed. | Root Directory must be `backend`; Build Command `pip install -r requirements.txt`. |
| Render deploy fails: *"No open ports detected"* or the health check times out | The start command doesn't bind `0.0.0.0:$PORT`. | Start Command: `uvicorn app:app --host 0.0.0.0 --port $PORT`. |
| Render: *"PYTHON_VERSION … invalid"* | A short version like `3.12` was used. | Use the full form, e.g. `3.12.8`. |
| New week shows on Vercel but not at `/api/pulses/latest` | Render didn't redeploy for a `site/pulses/` change. | Check that Build Filters include `site/pulses/**`. Click **Manual Deploy → Deploy latest commit** once. |
| Neither Vercel nor Render redeployed after the workflow's commit | Rare: the bot's push didn't reach them. | Click redeploy by hand once. If it keeps happening, add a free **Deploy Hook** (Vercel: Settings → Git → Deploy Hooks; Render: Settings → Deploy Hook), store each URL as a GitHub secret, and have the workflow call it. |
| `/api/status` says *"GitHub rate limit"* | Anonymous limit of 60/hour from a shared Render IP. | Add `GITHUB_TOKEN_READONLY` (§4.1). |
| `/api/status` says *"not set up yet"* | `pulse-check.yml` has never run, or the file name differs. | Run it once by hand. The file name must be exactly `pulse-check.yml`. |
| Vercel shows *404 NOT_FOUND* | Root Directory isn't `site`, or a build step produced an empty folder. | Settings → Build and Deployment → Root Directory `site`; Build Command overridden and empty. Redeploy. |
| Vercel shows an old week after a new commit | Browser cache. | The site loads data with `no-cache`, and `site/vercel.json` sets `Cache-Control: max-age=0, must-revalidate` for `/pulses/*`. Press **Refresh**. |
| Workflow fails at `git push`: *"Permission denied to github-actions[bot]"* | The workflow token is read-only. | The workflow sets `permissions: contents: write`. Also check Settings → Actions → General → Workflow permissions. |
| Workflow fails: *"the server rejected the API key"* / *"MCP server is not healthy after 120s"* | Wrong `PULSE_MCP_API_KEY` secret, or the MCP server didn't wake. | See README → Troubleshooting. These are unchanged from local runs. |
| All Render free services stopped mid-month | The 750 shared hours are used up. | Look for something pinging a service (an uptime monitor, a tab polling). Remove it. Service resumes on the 1st. |
