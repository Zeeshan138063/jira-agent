# Jira Agent: a guide for product managers

The Jira Agent is an assistant inside Claude that helps you write good Jira tickets. It checks
whether a ticket already exists, helps you fill in the gaps, suggests the right epic, and creates or
updates tickets for you. **It never changes anything in Jira without showing you first and getting your
"yes", and it can never delete anything.**

## What you need

- Access to your team's Jira (the same account you use every day).
- Claude with the Jira Agent installed. Your team admin gives you one install command
  ([INSTALL.md](INSTALL.md)); it takes a minute.
- About 5 minutes the first time, to connect Jira.

## First time: connect Jira (once)

1. Start Claude with the Jira Agent and say, for example: **"Let's work on Jira."**
2. It asks which Jira site and project to use, and where to keep its copy of your tickets. The
   suggested answers are fine. It remembers them for next time. If your admin gave you a team file
   (`jira-agent.preset.json`), the agent uses it and only asks where to keep your ticket copy.
3. A page opens in your browser. Follow it:
   1. Click **Open Atlassian's API token page** and sign in if asked.
   2. Click **Create API token** (the plain one, not "with scopes"). Name it `jira-agent`, choose how
      long it should last, click **Create**, then **Copy**.
   3. Back on the page, enter your Jira email, paste the token, tell it how long you chose, and click
      **Connect**.
4. The agent downloads a copy of your project's tickets so it can search them quickly. This takes
   about 10 seconds for every 600 tickets.

You will not be asked for the token again until it expires.

## Everyday use: just ask

| You say | What it does |
|---|---|
| "Is there already a ticket about password reset emails?" | Searches your project and shows the closest tickets, and whether they are done |
| "Groom PROJ-123" | Scores the ticket, lists what is missing, and drafts an improved version |
| "Create a story for exporting reports to CSV" | Asks a few quick questions, checks for duplicates, suggests the epic, drafts it |
| "Turn this spec into an epic with stories" | Proposes a breakdown, checks it against your project's habits, then creates it |
| "Which epics are still open?" | Lists them with how much is done |
| "Update my local copy of the tickets" | Refreshes the copy (it also does this by itself) |

You can paste text, a spec or meeting notes into the chat and ask it to turn them into tickets.

## What happens before anything changes

1. The agent shows you a **preview**: exactly what it will create or change, with before and after for
   edits.
2. You say **yes** or tell it what to change.
3. Only then does it write to Jira. If you change your mind, nothing has happened.

Claude may also ask your permission to run the agent's commands. The first time, choose
**"Always allow"** for the Jira Agent so you are not asked on every step. (Your admin can allow it
for everyone.)

## Your token and when it expires

- The token is stored in your computer's secure Keychain. The agent never sees it and it is never
  shown in the chat. Please never paste it into the chat yourself.
- The agent **reminds you 7 days before it expires.** To renew: create a new token on Atlassian's token
  page (same steps as above) and say **"Renew my Jira token."**
- Tokens last at most one year. If you did not tell the agent how long yours lasts, it assumes the
  maximum and reminds you near the one-year mark.
- To stop access at any time, delete the token on
  https://id.atlassian.com/manage-profile/security/api-tokens .

## Good to know

- The token gives the agent the same access you have in Jira, no more and no less.
- A copy of your project's ticket text is kept on your computer. Treat that computer like you treat
  Jira itself.
- Searches use the words in your tickets, so a ticket that describes the same thing in very
  different words can be missed. The agent shows its closest matches; use your judgement.
- If two tickets look like duplicates, the agent tells you and offers three choices: create anyway, link
  them as related, or update the existing one.

## If something goes wrong

| What you see | What to do |
|---|---|
| "Atlassian did not accept that email and token" | Use the email you sign in to Jira with, copy the whole token, and use the plain **Create API token** button. Make a new token if unsure. |
| "Auth required" or the agent says to sign in again | Your token expired or was removed. Create a new one and say "Renew my Jira token." |
| The agent says the copy of tickets is out of date | Say "Update my local copy of the tickets." |
| A ticket you just created is not found in searches | Give it a moment, then say "Update my local copy." |
| Anything else | Tell the agent what happened; it can run a check. If it persists, ask your admin and include the message you saw. |

## Cheat sheet

- **Ask for what you want in plain words.** One sentence is enough to start.
- **Read the preview before saying yes.**
- **Renew your token when reminded.**
- **Never share your token.**
