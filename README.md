# sensi-agent

Watches [Sensi](https://sensi.copeland.com/en-us) smart thermostats and sends a notification when
something changes:

| Event (`NOTIFY_EVENTS`) | Example |
|---|---|
| `hvac_on` | 🔥 Hallway: Heat on · Room 68.5°F, set to 70°F |
| `hvac_off` | 💤 Hallway: Heat off · Room 70°F, ran 12 min |
| `setpoint` | 🌡️ Hallway: Set temperature 70°F → 72°F · Room 68.5°F, hold on |
| `mode` | 🎛️ Hallway: Mode changed from heat to cool · Room 74°F, set to 76°F |

It also tells you when a thermostat has been offline for a while, and when it stops getting data
from Sensi or Sensi rejects its login, so silence never means it quietly broke.

- **Read-only:** it only listens. There's no code in it that can change a thermostat.
- **Changes while it was away are still found:** the last state is saved, and each connection starts
  with the full state. A heat cycle that started and finished during an outage can't be seen.
- **Room temperature alone doesn't notify,** since it moves all day. Assistants can read it.
- **Agent bus (optional):** `sensi_status` and `sensi_history` for an MCP client such as Hermes.

With a National Grid login it also [estimates what the heat costs](#heating-cost), weekly.

Notifications go to any [Apprise](https://github.com/caronc/apprise) URL or a Discord webhook. A
furnace can cycle several times an hour, so turn off `hvac_on` and `hvac_off` if that's too many, or
give this agent its own channel.

Sensi doesn't say who made a change. A set temperature change while the schedule is running can be
the schedule itself: the notification says `schedule running` or `hold on`, which is all there is to
go on.

## ⚠️ There is no official API

Sensi offers no API for consumer accounts. This agent uses the same cloud service as Sensi's own apps,
with an approach worked out by the community
[iprak/sensi](https://github.com/iprak/sensi) Home Assistant integration, so:

- It can stop working whenever Sensi changes something. You'd get a "No data" or "rejected the login"
  notification.
- It may not be what Sensi's terms of service have in mind. Read them, and use it only for your own
  account.
- It's a cloud connection: the thermostats' local HomeKit interface isn't used.

## The Sensi login

Sensi's login needs reCAPTCHA, which is why the agent can't sign in with a password. Instead, sign in
yourself once and give it the long-lived token that comes back.

The only web sign-in Sensi has is Sensi Manager, its paid service for commercial customers. The
agent is for your ordinary home account: the community integration's instructions use this page only
to get the token from that account's login, with no subscription. **Don't subscribe or enter a card:**
the token is issued during sign-in, so you can close the page after it.

1. In Chrome or Edge, open DevTools (F12), go to the **Network** tab, and sign in at
   <https://manager.sensicomfort.com> with the email and password you use in the Sensi app.
2. Find the request named `token?device=...`. It can appear twice: use the one with a **Response**.
3. Copy the `refresh_token` value, without quotes. That's `SENSI_REFRESH_TOKEN`.

Sensi's support pages say a "wrong email address or password" error on this page usually means no
paid subscription. If the page shows that error and no `token?device=` request has a `refresh_token`
in its response, this method doesn't work for your account.

It's a credential for your account, so treat it like a password. Sensi can replace it when it's used,
so the agent keeps the newest one in its data volume (`tokens.json`, owner-only) and never logs it.
Changing your password ends it: repeat the steps and set the secret again, and a new value takes over
from the saved one at the next deploy.

`SENSI_CLIENT_ID` and `SENSI_CLIENT_SECRET` identify the web app to Sensi, not you. They aren't in
this repository: copy `CLIENT_ID2` and `CLIENT_SECRET2` from `custom_components/sensi/auth.py` in the
iprak/sensi repository.

## Heating cost

With `NATIONALGRID_USERNAME` and `NATIONALGRID_PASSWORD` set, a weekly report gives how long each zone
ran and estimates the week's gas cost, and a message goes out when a new gas bill posts:

> 🔥 Gas last week: about $53
> Hallway: 25 h 12 min
> Upstairs: 11 h 54 min
> Boiler: 31 h 30 min in all, since zones can run together
> About $38 for heat (26 therms), $15 for hot water, cooking and fixed charges (3 therms)

**How it's worked out:**

1. **Run time:** the hours any thermostat called for heat. Thermostats are assumed to share one boiler
   or furnace, so two calling at once count once. AC isn't counted.
2. **Non-heating gas:** the three lowest of the last 12 bills, as therms a day. That's normally summer,
   when gas only goes to hot water and cooking.
3. **Burn rate:** a bill's therms minus the non-heating gas, divided by the hours the heat ran in that
   bill's period. The rated BTU/h isn't used, since a modulating boiler often runs well below it.
4. **Total cost:** the week's therms (heat plus non-heating gas), with delivery read off the last 12
   bills at that rate of use, which takes in the fixed charge and every tier, plus supply at the latest
   bill's price. Heat's share is what it adds to the cost of the non-heating gas alone.

`sensi_gas_cost` also gives the cost of one more therm, which is lower than heat's average cost because
delivery's cheapest tier is the last one reached: how much delivery rises per therm across the busier
half of the last 12 bills, plus the latest supply price.

**Limits to keep in mind:**

- **It's an estimate.** It works by month, because gas meters without AMI report no hourly data.
- **Costs start after the first bill with enough run time:** at least half the period watched and
  5 hours of heat. Before that the report gives hours only. Bills posted before the agent started
  have no run time to compare with.
- **Winter is likely a little high.** Hot water takes more gas when the incoming water is colder, and
  that's counted as heat.
- **The supply price lags,** since it comes from the latest bill. Each new bill corrects it.
- **Short heat cycles can be missed,** as noted under `RESYNC_SECONDS`.

**⚠️ National Grid has no official API for this either.** It uses the community
[py-nationalgrid](https://github.com/virtitnerd/py-nationalgrid) library, which signs in as you, so it
can break whenever National Grid changes something (you'd get a notification), and it may not be what
their terms of service have in mind. Use it only for your own account. It has been tested with a
downstate New York gas account, so other regions and accounts may differ.

The agent signs in about once a day, keeps only each bill's dates, therms, delivery and supply charges,
and doesn't store or log account numbers or addresses. The bills are in the data volume, and
`sensi_gas_cost` gives them to anything holding the agent bus token.

## Deployment

Every push to `main` runs the tests. The deploy job then sends the build context and a rendered
`agent.env` to the Docker host, where the root-owned `agent-deploy` (from a host prepared with
`deploy/docker-host.sh` in the dockerfile-agent repository) builds the image and replaces the
container, and rolls back if the new one doesn't stay up. Saved state and tokens are in the
`sensi-agent-data` volume.

To try it without deploying:

```bash
pip install -r requirements.txt
cp .env.example .env    # fill in the three SENSI_ values; add STATE_PATH and TOKENS_PATH under ./data
set -a; . ./.env; set +a
python -m sensi_agent
```

Then move a thermostat's set temperature by a degree: the change appears in the log.

### Self-hosted runners and public repositories

GitHub advises against attaching self-hosted runners to public repositories, because a pull request
from a fork could run code on your hardware. This workflow runs tests on GitHub-hosted runners and
never runs the deploy job for pull requests, but to be safe deploy from a **private** copy, or require
approval for fork pull request workflows.

## Secrets and variables

This repository contains **no secrets**. Set these under **Settings → Secrets and variables → Actions**.

### Secrets

| Name | Required | Expected value |
|---|---|---|
| `SENSI_REFRESH_TOKEN` | **yes** | The token from [The Sensi login](#the-sensi-login): a long string starting `eyJ`. |
| `SENSI_CLIENT_ID` | **yes** | The web app's client id, see above. |
| `SENSI_CLIENT_SECRET` | **yes** | The web app's client secret, see above. |
| `NOTIFY_URLS` | no | Apprise URLs or Discord webhook URLs, comma separated. Without any, changes are only logged. |
| `NATIONALGRID_USERNAME` | no | Your National Grid online account's sign-in. Set both to turn on [Heating cost](#heating-cost). |
| `NATIONALGRID_PASSWORD` | no | That account's password. |
| `AGENT_BUS_TOKEN` | no | A long random token (`openssl rand -base64 32`). Turns on the agent bus. |
| `PUBLIC_DEPLOY_KEY` | no | Only for mirroring a private copy to a public one: the private half of an SSH key whose public half is a write deploy key on the public repository. |

### Variables

| Name | Required | Expected value |
|---|---|---|
| `AGENT_HOST` | **yes** | Address of the Docker host. |
| `PRIORITY` | no | `high` (default) or `low`. Picks the deploying runner and sets the container label `homelab.priority`. |
| `NOTIFY_EVENTS` | no | Any of `hvac_on`, `hvac_off`, `setpoint`, `mode`, comma separated (default all). Everything is still kept for `sensi_history`. |
| `OFFLINE_GRACE_MINUTES` | no | How long a thermostat or the cloud must be gone before it's reported (default 15). Sensi can take about 10 minutes to mark a thermostat offline. |
| `RENOTIFY_HOURS` | no | Reminder interval for open problems (default 24). |
| `RESYNC_SECONDS` | no | Reconnect for a full state when the cloud has sent nothing for this long (default 60, at least 30). Sensi appears to send state only when a connection starts, so changes are seen on this cycle (about 5 s longer than the value), and a heat cycle shorter than it can be missed. |
| `HISTORY_SIZE` | no | Changes kept for `sensi_history` (default 500). |
| `GAS_REPORT_DAY` | no | Day of the weekly heating cost report (default `sun`). |
| `GAS_REPORT_HOUR` | no | Hour of that report, 0 to 23 (default 8). |
| `BUS_HOST_PORT` | no | Host port for the agent bus. Empty publishes nothing. |
| `AGENT_BUS_PORT` | no | The bus port inside the container (default 8765). |
| `BOT_ALERT_URL` | no | Where to hand problem alerts to an assistant first. Changes always go to `NOTIFY_URLS`. |

## Agent bus

With `AGENT_BUS_TOKEN` set, `http://<host>:<BUS_HOST_PORT>/mcp` serves these tools, all read-only:

| Tool | Returns |
|---|---|
| `sensi_status` | Each thermostat's room temperature, humidity, mode, set temperatures, and what's running |
| `sensi_history` | Recent changes, optionally for one thermostat |
| `sensi_gas_cost` | With a National Grid login: hours per zone and for the boiler, estimated therms and total cost for a number of days, and recent bills |
| `agent_status` | Revision, uptime and open problems |

## Troubleshooting

- **"Sensi rejected the login":** get a new token as above and set `SENSI_REFRESH_TOKEN`.
- **"No data from Sensi's cloud":** check Sensi's app first. If the app works, Sensi may have changed
  something: see the iprak/sensi issues.
- **Changes in the app sometimes don't apply while the agent runs:** iprak/sensi notes this happens
  occasionally with two sessions on one account.
- **"National Grid rejected the login":** check the two secrets. It retries once a day, so a wrong
  password can't lock the account quickly.
- **"No gas bill data":** the library may need updating: see the py-nationalgrid issues.
- **Temperatures show the wrong unit:** the agent reports what the thermostat is set to display.
