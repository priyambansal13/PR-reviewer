# Setting up the GitHub App

The reviewer acts as a **GitHub App**, not as your personal account. That means:
- it gets only the permissions listed below, and only on the repos you pick;
- its comments show up as `your-app-name[bot]`;
- it gets short-lived tokens (1 hour) instead of a long-lived personal token.

It takes about 5 minutes. The stack must be running (`./setup.sh start`).

## 1. Get a public URL

```bash
./setup.sh tunnel
```

This prints something like `Webhook URL: https://some-words.trycloudflare.com/webhook/github`. Keep it for step 2.

> The free quick-tunnel URL **changes every time** the tunnel restarts. Once the App exists, `./setup.sh tunnel` updates
> the App's webhook URL for you, so you only paste it by hand this once.

## 2. Create the App

Open **https://github.com/settings/apps/new** and fill in:

| Field | Value |
|---|---|
| GitHub App name | Something unique, e.g. `priyam-pr-reviewer` (this becomes the bot's name) |
| Homepage URL | `https://github.com/priyambansal13/PR-reviewer` |
| Callback URL / Setup URL / Device flow | leave empty / unticked |
| **Webhook → Active** | ✅ |
| **Webhook URL** | the *Webhook URL* from step 1 |
| **Webhook secret** | the value of `GITHUB_WEBHOOK_SECRET` in `pr-reviewer/.env` (copy it with `grep GITHUB_WEBHOOK_SECRET .env \| cut -d= -f2 \| pbcopy`) |
| SSL verification | Enable |

**Repository permissions** (leave everything else as *No access*):

| Permission | Access | Why |
|---|---|---|
| Pull requests | **Read and write** | read PRs and diffs; post review comments |
| Contents | **Read-only** | compare commits and read files for context |
| Metadata | Read-only | mandatory, set automatically |

**Subscribe to events:** tick **Pull request**. Install/uninstall events are sent automatically.

**Where can this GitHub App be installed?** → *Only on this account*.

Click **Create GitHub App**.

## 3. Save the App ID and private key

On the App's settings page:

1. Copy the **App ID** (a number near the top) into `.env`:
   ```
   GITHUB_APP_ID=1234567
   ```
2. Scroll to **Private keys** → **Generate a private key**. A `.pem` file downloads. Move it into the project:
   ```bash
   mv ~/Downloads/*.private-key.pem secrets/github-app.pem
   chmod 600 secrets/github-app.pem
   ```
   `secrets/` is git-ignored. Treat this file like a password: anyone holding it can act as the App.

Restart so the containers pick up the new `.env`:

```bash
./setup.sh start
```

## 4. Install the App on a test repo

App settings page → **Install App** (left menu) → your account → **Only select repositories** → pick the repo(s) the
bot should review → **Install**.

## 5. Check it

```bash
./setup.sh github
```

You should see the App name, its permissions, the webhook URL, and the repos it's installed on. The command also syncs
those repos into the database.

Then open a PR on the test repo (any small change) and watch:

```bash
./setup.sh logs orchestrator-worker
```

In Phase 2 the worker fetches the diff and logs which files it would review and which it skips, with line counts.
The review itself arrives in Phase 3.

## Troubleshooting

| Symptom | Fix |
|---|---|
| App page → *Advanced* → Recent Deliveries shows **401** | Webhook secret in the App ≠ `GITHUB_WEBHOOK_SECRET` in `.env`. Fix either one, or run `./setup.sh tunnel` (it re-sends the secret). |
| Deliveries fail with a connection error | The tunnel is down or the URL changed: `./setup.sh tunnel` |
| `private key not found` in worker logs | The `.pem` isn't at `secrets/github-app.pem` |
| `could not get installation token … 401` | Wrong `GITHUB_APP_ID`, or a key from a different App |
| `… 404` fetching the PR | The App isn't installed on that repo, or lacks *Pull requests: Read* |
| Run status `skipped` with "PR head moved" | Expected: a newer commit was pushed, and that commit gets its own run |

You can resend any delivery from App settings → **Advanced** → *Recent Deliveries* → **Redeliver**. It's handy for
testing; repeated deliveries are recognised as duplicates.
