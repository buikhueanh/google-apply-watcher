# Google Apply Button Watcher

Google posted a new grad role with no Apply button. This watches the page and pushes me a Telegram message within 5 minutes of the button going live, running on GitHub Actions so it does not need my laptop.

Target: [Software Engineer, Early Career, Campus](https://www.google.com/about/careers/applications/jobs/results/78703249065943750-software-engineer-early-career-campus) (`78703249065943750`)

```bash
python3 watcher.py --selftest    # is the detector still valid?
python3 watcher.py --testnotify  # can a message actually reach me?
python3 watcher.py --once        # one check (this is what CI runs)
python3 watcher.py --loop 60     # poll locally
python3 test_watcher.py          # offline tests
```

---

## How I found the signal

### First hypothesis, and why it was wrong

I fetched the target page and a control page (a job that *is* open) and diffed them. The open one contained:

```
https://www.google.com/about/careers/applications/apply?jobId=CiUAL2Fck...%3D%3D_V2&loc=PL&title=...
```

The target had nothing like it. Conclusion: the page is server-rendered, the Apply anchor is a plain `<a>`, and detection is a substring match. No headless browser needed.

That conclusion was right. The regex I wrote from it was not:

```python
r'https://www\.google\.com/about/careers/applications/apply\?[^"\']+'
```

It matched nothing against the live page.

**The mistake:** the tool I used to read the page executed JavaScript, so I was reading rendered output and calling it raw HTML. Two different artifacts. I had validated the idea against something other than what the script would actually see.

### Diagnosing it

Rather than tweak the regex until something matched, I dumped what the script itself was receiving:

```
bytes:           1,147,632
apply-path hits: 0
the word Apply:  4
```

A full 1.1MB page, so not a consent wall or a redirect. And `Apply` appeared four times, so the content was there. The URL just was not in the form I expected. Printing the surrounding characters gave it away:

```html
<a class="WpHeLc ..." href="apply?jobId=CiUA...%3D%3D_V2&amp;loc=PL&amp;title=Senior+Software+Engineer"
   aria-label="Apply" id="apply-action-button">
```

The page declares `<base href="https://www.google.com/about/careers/applications/">` and writes the link **relative**. My regex demanded the absolute form, which only exists after the browser resolves it. Ampersands were HTML-escaped too.

**Generalizes to:** when a scraper finds nothing, check what it received before changing how you parse. The failure is usually upstream of the parser.

### The bug that would have mattered more

The same diagnostic run compared both pages on token counts:

| | open job | my target (closed) |
|---|---|---|
| `apply?jobId=` tokens | 22 | **20** |

A closed page already contains twenty apply links. They belong to the other postings inlined in the sidebar results list, sitting inside a JSON blob with `&`-escaped ampersands.

So the obvious fix, searching for `apply?jobId=`, would have reported **OPEN on the first run and every run after**. Worse than useless: I would have checked the page manually, seen no button, and stopped trusting the tool.

The signal that actually separates the two states is the button element. The live Apply control carries `id="apply-action-button"`, and nothing else on the page does.

**Generalizes to:** a scraper's hard problem is usually the false positive, not the false negative. A missing alert is obvious. A wrong alert quietly destroys the only thing the tool has, which is your trust in it.

---

## Design decisions

**Read both attributes off the same element.** `detect()` iterates `<a>` tags and requires the button id and the href on one tag. Checking that both strings exist *somewhere* in a 1.1MB document is the same false-positive bug wearing a different hat. There is a test for exactly this case.

**No browser.** One GET plus a regex is ~200ms. Playwright would be ~2s and ~200MB per check, and would turn a free-tier cron into something I would have to think about. Worth confirming the raw HTML is enough before reaching for a renderer.

**A canary target.** `--selftest` runs the detector against a job known to be open. Without it, "the button is not up yet" and "my parser broke and I have not noticed" produce identical output. That distinction was not theoretical: the canary is what caught the relative-URL bug. It runs on every CI invocation, not just at setup, because Google can change their markup at any time.

**Edge-triggered notifications.** State lives in `state.json`, cached between runs. Alerts fire on the `closed -> open` transition only. Level-triggered would mean 288 messages a day.

**Notifiers are independent and fail-isolated.** Five channels (Telegram, Discord, email, Twilio SMS, macOS banner), each a plain function that no-ops when its env vars are absent. One dead channel cannot suppress the others. This system fires once, ever, so degrading beats aborting.

**Secrets never touch the repo.** Credentials come from environment variables, `.env` is gitignored, CI reads from GitHub Secrets. The repo is public because Actions is free and unlimited on public repos, which only works if nothing sensitive is committed.

**5-minute cadence, deliberately.** My first workflow looped internally for 4.5 minutes per run to get 60-second resolution. I dropped it: 288 runs a day at 5 minutes each is roughly 24 hours of compute per day, and on a private repo it would exhaust the 2,000 free minutes in three days. The extra precision buys nothing, because an application window stays open for days once it opens. Latency should be budgeted by how fast you need to *act*, not by how fast you *can* poll.

**Tested what the log cannot tell you.** A green CI run only proves detection ran. It says nothing about whether a message can leave GitHub and reach my phone, and that path only gets exercised at the one moment it has to work. I temporarily pointed the watcher at an open job, confirmed a real alert arrived, then reverted.

---

## Architecture

```
watcher.py
  fetch(url)              -> str          network, isolated
  detect(key, url, html)  -> Result       pure, unit tested offline
  notify_*(subject, body)                 independent channels
  run_once()                              state diff + fan-out
```

`detect()` is split from `fetch()` so the parsing logic can be tested against saved fixtures with no network. Anything that cannot be tested reliably will rot, and the parser is the part most likely to break.

## Setup

Telegram: message `@BotFather`, `/newbot`, copy the token, message your new bot once (it cannot message you first), then read your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`.

```bash
export TELEGRAM_TOKEN='...'
export TELEGRAM_CHAT_ID='...'
python3 watcher.py --testnotify
```

For CI: add both as repository secrets and put `watch.yml` at `.github/workflows/watch.yml`.

| Channel | Environment variables |
|---|---|
| Telegram | `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` |
| Discord | `DISCORD_WEBHOOK`, `DISCORD_USER_ID` |
| Email | `SMTP_USER`, `SMTP_PASS`, `NOTIFY_EMAIL` |
| Twilio SMS | `TWILIO_SID`, `TWILIO_TOKEN`, `TWILIO_FROM`, `TWILIO_TO` |
| macOS banner | `NOTIFY_MACOS=1` |

Adding another posting is one line in `TARGETS`.

## Known limits

- `id="apply-action-button"` is an implementation detail of Google's frontend. If they rename it, `--selftest` fails loudly in the CI log rather than the watcher going quietly blind. That is the intended failure mode, not a fix.
- If Google ever gates the anchor behind sign-in, an unauthenticated GET would see a false negative. Not observed on any page tested.
- GitHub disables scheduled workflows after 60 days of repository inactivity.
- The `jobId` token is opaque and server-issued. It cannot be derived from the numeric job id, so there is no way to apply before the button exists. Waiting is the only option, which is why this exists.
