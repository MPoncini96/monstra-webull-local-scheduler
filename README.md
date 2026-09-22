# Monstra → Webull Rebalancer (local scheduler)

Pulls your target portfolio weights from Monstra and rebalances a Webull
account to match, on whatever schedule you set up on your own machine.

**This runs on your computer (or a machine you control), not on a hosted
service** — same reasoning as the Schwab local-scheduler package. Webull's
OpenAPI ties your `app_key`/`app_secret` to your account through a one-time
approval step (their SDK refers to the cached result as a "2FA token"), and
that token is cached to a local file that has to survive between runs. A
hosted "serverless" cron service without a persistent disk isn't a good fit
for that; your own machine is.

Unlike the Schwab and Alpaca scripts, this one is **not** dependency-free —
Webull's OpenAPI requests are signed with a custom HMAC scheme, and
reimplementing that by hand from scratch would be guesswork. This uses
Webull's own official SDK, [`webull-openapi-python-sdk`](https://github.com/webull-inc/webull-openapi-python-sdk).

## What it does

1. `GET /api/v1/portfolio/latest` — pulls your Monstra target weights.
2. Reads your current Webull account value (`get_account_balance`) and
   positions (`get_account_position`) via the official SDK.
3. Computes the buy/sell orders needed to move your Webull account toward
   those target weights, pricing unheld target symbols via the Market Data
   API's snapshot endpoint.
4. Prints and logs the plan. Orders are only submitted once `DRY_RUN=false`
   **and** you type `EXECUTE` at an interactive confirmation prompt — a
   scheduled/unattended run can never place live trades, even with
   `DRY_RUN=false`, because there's no one there to type `EXECUTE`. It will
   log a warning and stop instead.

## One-time setup

1. Get a Monstra API key at https://www.monstra.bot/dashboard/api.
2. Apply for OpenAPI access at https://developer.webull.com. Webull says
   production review typically takes 1–2 business days; the **sandbox**
   environment works immediately with Webull's own shared test accounts —
   start there (`WEBULL_ENV=sandbox`, the default) before ever pointing this
   at `production`.
3. Copy `.env.example` to `.env` and fill in `MONSTRA_API_KEY`,
   `WEBULL_APP_KEY`, `WEBULL_APP_SECRET`. Leave `WEBULL_ACCOUNT_ID` blank for
   now. Alternatively, edit the constants directly at the top of `main.py`.
4. **Set these as real, persistent environment variables**, not just values
   in a `.env` file — a scheduled task/cron job does not read `.env` files
   automatically, and this script does not load one for you.
5. Install the SDK: `pip install -r requirements.txt`
6. Run once, interactively, to list your accounts:
   ```
   python main.py
   ```
   With `WEBULL_ACCOUNT_ID` unset, it prints every account this app key can
   see (id, type, label, account number) and exits. Copy the `account_id` for
   the account you want to trade into `WEBULL_ACCOUNT_ID`.
7. Run again. The first real run may need you to approve the new app/device
   from your Webull mobile app — the SDK waits (polls) for that approval for
   a few minutes. Once approved, a token is cached locally and reused on
   later runs, including unattended scheduled ones, until it expires.
8. Confirm a run works without prompting for approval again, then set up the
   scheduler.

## Windows: Task Scheduler

1. Set persistent environment variables so the scheduled task can see them:
   ```
   setx MONSTRA_API_KEY "your-key-here"
   setx WEBULL_APP_KEY "your-webull-app-key"
   setx WEBULL_APP_SECRET "your-webull-app-secret"
   setx WEBULL_ACCOUNT_ID "your-account-id"
   setx WEBULL_ENV "sandbox"
   ```
   (Open a new terminal afterward — `setx` only affects future processes.)
2. Open **Task Scheduler** → **Create Basic Task…**
3. Name it (e.g. "Monstra Webull Rebalance"), choose a trigger (e.g. Daily,
   after market close).
4. Action: **Start a program**.
   - Program/script: full path to `python.exe` (find it with `where python`).
   - Add arguments: `main.py`
   - Start in: the folder containing `main.py`.
5. Finish, then run the task once manually and check `webull_rebalance.log`
   next to the script to confirm it worked.

Command-line equivalent:

```
schtasks /create /tn "Monstra Webull Rebalance" /tr "python C:\path\to\main.py" /sc daily /st 16:15
```

## macOS: launchd

Create `~/Library/LaunchAgents/bot.monstra.webull-rebalance.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>bot.monstra.webull-rebalance</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/main.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>MONSTRA_API_KEY</key><string>your-key-here</string>
    <key>WEBULL_APP_KEY</key><string>your-webull-app-key</string>
    <key>WEBULL_APP_SECRET</key><string>your-webull-app-secret</string>
    <key>WEBULL_ACCOUNT_ID</key><string>your-account-id</string>
    <key>WEBULL_ENV</key><string>sandbox</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>16</integer>
    <key>Minute</key><integer>15</integer>
  </dict>
  <key>StandardOutPath</key><string>/tmp/monstra-webull-rebalance.log</string>
  <key>StandardErrorPath</key><string>/tmp/monstra-webull-rebalance.log</string>
</dict>
</plist>
```

Load it with `launchctl load ~/Library/LaunchAgents/bot.monstra.webull-rebalance.plist`.

Or, simpler, a crontab entry (`crontab -e`) — cron's environment is minimal,
so export the variables in the line itself or a wrapper script:

```
15 16 * * 1-5 MONSTRA_API_KEY=... WEBULL_APP_KEY=... WEBULL_APP_SECRET=... WEBULL_ACCOUNT_ID=... WEBULL_ENV=sandbox /usr/bin/python3 /path/to/main.py >> /path/to/webull_rebalance.log 2>&1
```

## Config reference (env vars)

| Variable | Default | Notes |
|---|---|---|
| `MONSTRA_API_KEY` | — | required |
| `WEBULL_APP_KEY` / `WEBULL_APP_SECRET` | — | required, from developer.webull.com |
| `WEBULL_ACCOUNT_ID` | — | required after the first discovery run |
| `WEBULL_ENV` | `sandbox` | `sandbox` or `production` |
| `DRY_RUN` | `true` | log the plan only; set `false` to place real orders |
| `MIN_TRADE_DOLLARS` | `25` | skip trades smaller than this |

## Ongoing maintenance

- Webull's cached token can expire or be revoked. When that happens, the
  scheduled run will fail (check `webull_rebalance.log`) and you'll need to
  re-run `python main.py` interactively and re-approve from the Webull app.
- Check `webull_rebalance.log` (next to the script) periodically — that's
  where scheduled runs record what they saw and did.

## Safety

- `WEBULL_ENV` defaults to `sandbox` — it will not touch a real account
  unless you explicitly set `WEBULL_ENV=production`.
- `DRY_RUN` defaults to `true`. Even with `DRY_RUN=false`, orders are only
  ever submitted from an interactive run where you type `EXECUTE`.
- Orders are MARKET/DAY orders that can size to a fractional share count, so
  a target weight is matched instead of rounding down to whole shares.
  Webull only allows a fractional (<1 share) quantity in a single order, so
  a trade spanning a whole-share boundary is split into a whole-share order
  plus a separate fractional order.
- Sells are capped at your current share count — this never opens a short
  position.
- Trades below `MIN_TRADE_DOLLARS` are skipped. A fractional leg is also
  skipped if it's below Webull's own $5 fractional-order minimum — a
  platform constraint this script can't work around, so a dust-sized
  fractional remainder can occasionally get stuck (logged when it happens).
- **This script places real trades with real money once `WEBULL_ENV=production`,
  `DRY_RUN=false`, and you confirm interactively.** Test thoroughly against
  `WEBULL_ENV=sandbox` first. Review the printed/logged plan carefully.
- Treat your Monstra API key and Webull app credentials like passwords —
  don't commit them to source control.
